# Configuration reference

watchable is entirely configured from one YAML file, conventionally
`config.yaml`. Start from `config.example.yaml` at the repo root and copy it.

```
cp config.example.yaml config.yaml
```

## Top level

```yaml
database:
  path: ./data/watchable.db   # SQLite file; created (and its parent dir) if missing

backup: { ... }                # optional; see below

log_level: INFO               # DEBUG | INFO | WARNING | ERROR

servers: { ... }              # see below
users: [ ... ]                # see below
sync: { ... }                 # see below
```

## `servers`

A mapping of **server key** (an internal name you choose, referenced by
`users[].accounts` and `sync.pairs[].source/target`) to that server's
connection details.

```yaml
servers:
  plex_main:
    type: plex                # plex | jellyfin | emby
    url: https://plex.lan:32400
    token: ${PLEX_TOKEN}      # required for type: plex
    verify_tls: false         # default true; false for self-signed local certs
    timeout_seconds: 15       # default 15

  jellyfin_main:
    type: jellyfin
    url: http://jellyfin.lan:8096
    api_key: ${JELLYFIN_API_KEY}   # required for type: jellyfin / emby
```

**Plex is a special case.** Plex scopes watch state to whichever token makes
the request -- there's no "read user X's watched status" call an admin token
can make on someone else's behalf. So for a Plex server, every synced
person's `users[].accounts.<plex_server_key>` entry (below) must be *their
own* Plex token, not a username. See `docs/PROVIDERS.md#plex` for how to get
one. Jellyfin and Emby don't have this restriction: one API key can act on
any user's data, so their `accounts` entries are just that user's account ID.

## `users`

Maps one real person to their account identifier on each server they should
be synced on. A person only participates in a given sync pair if they have
an entry for *both* of that pair's servers -- if `alex` only has a
`plex_main` entry and a pair is `plex_main -> jellyfin_main`, nothing syncs
for them on that pair (silently skipped, not an error, since a partial
rollout across servers is a normal setup).

```yaml
users:
  - name: alex
    accounts:
      plex_main: <alex's own plex token>
      jellyfin_main: <alex's jellyfin user id, from Jellyfin's admin dashboard>
      emby_main: <alex's emby user id>

  - name: sam
    accounts:
      jellyfin_main: <sam's jellyfin user id>
      emby_main: <sam's emby user id>
      # sam has no plex_main entry -- fine, they just don't sync on any
      # pair involving plex_main
```

## `sync`

```yaml
sync:
  conflict_strategy: most_watched   # default for bidirectional pairs; see below
  pairs: [ ... ]                    # required, at least one
  schedule:
    interval_minutes: 30            # optional; only used by `watchable run`
```

### `sync.pairs[]`

Each pair is one directional or bidirectional relationship between two
servers.

```yaml
pairs:
  - name: plex-to-jellyfin        # free-form label, shown in logs
    source: plex_main             # a servers[] key
    target: jellyfin_main         # a servers[] key, must differ from source
    direction: one-way            # one-way | bidirectional (default: one-way)
    media_types: [movie, episode] # default: both
    conflict_strategy: latest     # optional: overrides the global default, bidirectional pairs only
```

- **`one-way`**: `source`'s watch state is authoritative. Watching something
  on `target` never flows back to `source`; it only ever gets overwritten by
  `source`'s state next time they diverge.
- **`bidirectional`**: either server can be the one that changed since the
  last sync. When only one side has state for an item, it's copied to the
  other side (no conflict). When both sides have *differing* state, the
  pair's (or global) `conflict_strategy` decides which side wins.

You can define more than one pair between the same two servers with
different `direction`/`media_types` if you want, e.g. movies one-way but
episodes bidirectional -- though usually a single bidirectional pair
covering everything is simpler.

### `conflict_strategy`

Only matters for `bidirectional` pairs where both sides changed:

| Value | Behavior |
|---|---|
| `most_watched` (default) | Whichever side progressed further wins (played beats in-progress, higher offset beats lower). Never rewinds progress. |
| `latest` | Whichever side has the more recent last-played timestamp wins. |
| `source_wins` | The pair's `source` always wins ties, but it's still a genuine bidirectional pair for the "only one side has state" case. |
| `target_wins` | Same, but `target` always wins. |

### `sync.schedule.interval_minutes`

Only read by `watchable run` (a long-lived loop -- see the Docker image in
`docker/`). `watchable sync` always runs once and exits; if you're running
watchable via cron or a systemd timer instead of the container, leave this
unset and let the scheduler own the interval.

## `backup`

```yaml
backup:
  dir: ./data/backups     # default: ./data/backups
  interval_hours: 24      # optional; only read by `watchable run`
  keep_days: 30           # default: 30; null disables purging
```

A backup is a full point-in-time copy of the database file (`items`,
`item_guids`, `watch_state`, and `sync_log` together), taken with sqlite3's
own backup API so it's always a consistent snapshot even if a sync is
mid-write. Restoring one is an all-or-nothing rollback to that moment --
there's no partial "just the watch state" restore, since `watch_state` rows
only make sense alongside the item/guid rows they reference.

- `watchable backup create config.yaml` -- take a backup right now,
  regardless of these settings, then purge anything older than `keep_days`.
- `watchable backup list config.yaml` -- list backups, newest first.
- `watchable backup restore config.yaml <file>` -- overwrite the live
  database with a backup (prompts for confirmation unless `--yes`). Make
  sure no `sync`/`run` is running concurrently first.
- `watchable backup purge config.yaml [--older-than-days N]` -- delete
  backups older than `keep_days` (or `N`, if given).
- `interval_hours`: if set, `watchable run` also takes a backup on this
  cadence -- independent of `sync.schedule.interval_minutes` -- and purges
  old ones afterwards. Unset (the default) means `run` never backs up on
  its own; only explicit `watchable backup create` calls do.
- `keep_days`: backups older than this are deleted every time a backup is
  taken (by `run`'s schedule or `backup create`). `null` keeps every backup
  forever.

## Secrets and environment variables

Any string value in the YAML can reference an environment variable:

- `${VAR}` -- expanded to `$VAR`'s value; **loading fails** if it's unset.
- `${VAR:-default}` -- expanded to `$VAR`'s value, or `default` if unset.

This is meant to keep tokens/API keys out of `config.yaml` itself, so the
file is safe to keep in version control (a private repo, or a dotfiles
repo) with real secrets supplied at runtime -- via your shell, a systemd
`EnvironmentFile`, or the `env_file:` key in `docker/docker-compose.example.yml`.

## Validating a config

```
watchable validate config.yaml
```

Checks the file loads, all referenced env vars resolve (or have defaults),
every `type` is one of `plex`/`jellyfin`/`emby` with the right credential
field set, every pair's `source`/`target` reference a defined server, and
prints a summary. This does **not** contact any server -- for that, see
`watchable test-connections config.yaml`.
