# Architecture

## Why a local database, and not server-to-server syncing?

The obvious design for "sync watch state between servers" is direct:
watch something on Plex, immediately push that state to Jellyfin and Emby.
watchable doesn't do this, for a few reasons:

- **Bidirectional pairs need a merge point.** If Plex and Jellyfin can both
  change independently and both feed each other directly, there's no single
  place to decide who wins when they disagree -- you get two servers each
  convinced the other one is stale. Pulling both sides into one local store
  first gives the conflict-resolution logic one consistent view to compare
  against.
- **A poll-based model doesn't need every server always reachable.** Direct
  syncing assumes all your servers are up whenever any one of them changes.
  In a home lab that's often not true (a Pi rebooting, a server offline for
  maintenance). Pull-then-push means a run degrades to "sync whatever's
  currently reachable" instead of failing outright.
- **There's somewhere to audit from.** Every push watchable makes is logged
  against the local item/user/server it changed (`sync_log`), so "why did
  this get marked watched" has an answer.

## The two-phase run

Every `watchable sync` invocation is two phases, always in this order (see
`watchable/sync.py`):

1. **Pull.** For every server referenced by any configured pair, for every
   configured user with an account on it, fetch their current watch state
   and record it into the local SQLite database (`watch_state` table).
   Nothing is written back to any server in this phase.
2. **Reconcile + push.** For every sync pair, for every user present on both
   sides of that pair, compare what's now stored locally for the source and
   (for bidirectional pairs) target server, and push whichever side needs to
   change.

Keeping these separate means every decision in phase 2 is made against one
consistent snapshot from phase 1, rather than an earlier push in the same run
changing what a later step reads back.

## Cross-server item identity

No two media servers agree on an item's internal ID. The only reliable way
to know that Plex's `ratingKey=4821` and Jellyfin's `itemId=e3f1...` are "the
same movie" is external identifiers -- IMDb, TMDb, TVDb -- which is what
`watchable/matching.py` and the `items` / `item_guids` tables are built
around:

- A movie's matching key is `"<scheme>:<value>"` for each external id it has
  (`imdb:tt0111161`, `tmdb:278`, ...). An item with two of the three schemes
  gets two keys pointing at the same database row, so a target server that
  only exposes one of the schemes still matches.
- A TV episode's matching key is the *show's* external id(s) plus season and
  episode number (`imdb:tt0903747:S01E07`), since episodes themselves
  rarely carry their own imdb/tmdb id.
- If an item somehow can't be resolved to any external id at all (a home
  video, an obscure title an agent couldn't match), watchable skips it --
  there's no safe way to match it across servers, so it's left alone rather
  than guessed at.
- If two servers are pulled in an order where the same item is first seen
  under only a tmdb id and later under only an imdb id, and a third server
  later reports *both*, `Database.resolve_or_create_item` merges the two
  existing item rows (and their watch-state history) into one.

## Reconciliation logic

For one (item, user, pair):

| Pair direction | Source has state | Target has state | Result |
|---|---|---|---|
| one-way | no | -- | nothing to do |
| one-way | yes | no | push source -> target (if target's library has a matching item) |
| one-way | yes | yes, differs | push source -> target |
| one-way | yes | yes, same | no-op |
| bidirectional | no | yes | push target -> source |
| bidirectional | yes | no | push source -> target |
| bidirectional | yes | yes, same | no-op |
| bidirectional | yes | yes, differs | conflict strategy decides the direction |

"Differs" ignores view-offset differences under 30 seconds (`sync.py`'s
`VIEW_OFFSET_TOLERANCE_MS`) -- otherwise every run would re-push over normal
seek/buffering noise.

Conflict strategies (`watchable.config.ConflictStrategy`), used only when
both sides of a bidirectional pair have diverging state:

- `most_watched` (default): whichever side has progressed further wins --
  played beats in-progress, and a higher view offset beats a lower one. This
  never "rewinds" someone's progress.
- `latest`: whichever side has the more recent last-played timestamp wins.
- `source_wins` / `target_wins`: a fixed side always wins, useful when one
  server is the trusted source of truth even inside a nominally
  bidirectional pair.

When a push target doesn't have a locally-known watch-state row yet (never
observed there before), the target server's library is searched by guid
(`MediaServerClient.find_item_by_guids`) to see if the item exists there at
all. If it doesn't (the item was never added to that server's library), the
push is skipped and logged, not treated as an error.

## Provider interface

Every media server is one class implementing
`watchable.providers.base.MediaServerClient`:

- `test_connection()` -- used by `watchable test-connections`.
- `list_users()` -- accounts on the server (for `watchable`'s own reference;
  the sync engine itself uses the config's `users[].accounts` mapping).
- `iter_watch_state(server_user_id, media_types)` -- every item this user
  has touched (played, or has partial progress).
- `set_watch_state(server_user_id, server_item_id, *, played, view_offset_ms,
  runtime_ms)` -- push a state onto the server.
- `find_item_by_guids(media_type, item_guids, episode)` -- resolve where an
  item (known from another server) lives here, or `None`.

Adding a fourth media server means writing one new class against this
interface and registering it in `watchable/providers/__init__.py` --
`watchable/sync.py` never talks to a specific provider's HTTP API directly.
See `docs/PROVIDERS.md` for what each existing implementation actually does
against each server's real API, including the places where Plex in
particular works differently enough from Jellyfin/Emby to matter.

## Database schema

See the `SCHEMA` string at the top of `watchable/db.py` for the literal DDL.
In short:

- `items` / `item_guids` -- canonical library items and every external id
  known to point at them.
- `user_server_accounts` -- config-level person -> server account mapping
  (mirrors `config.yaml`'s `users[].accounts`, kept in the DB mainly so
  other tooling querying the DB directly doesn't need the YAML too).
- `watch_state` -- one row per (item, user, server): the state last observed
  there. This is both the pull phase's write target and the reconcile
  phase's read source.
- `sync_log` -- append-only audit trail of every push (and skip) watchable
  has made, surfaced by `watchable log`.

## Backups

`watchable/backup.py` snapshots and restores the whole database file (all
four tables above, together) via sqlite3's own backup API rather than a
plain file copy, so a snapshot taken while a sync is mid-write (WAL mode)
is still a consistent point-in-time copy rather than a torn one. A restore
is the same operation in reverse: the backup file is copied back over the
live database, and any leftover `-wal`/`-shm` sidecar files next to it are
removed so stale WAL frames from before the restore can't get replayed
onto it.

Restoring is deliberately all-or-nothing rather than a targeted
"restore just `watch_state`" -- a partial restore could leave `watch_state`
rows pointing at `item_id`s that `resolve_or_create_item` has since merged
or repointed, silently attaching old watch state to the wrong item.
Rolling back the whole file avoids that class of bug entirely.

`watchable run`'s loop tracks the sync schedule and the backup schedule
(`backup.interval_hours`) independently, waking for whichever is due next
-- a backup cadence shorter or longer than the sync interval both work.
`watchable backup create`/`list`/`restore`/`purge` operate the same code
path on demand, for cron-driven setups that don't use `run` at all.
