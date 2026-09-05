# Provider notes

How each client in `watchable/providers/` maps onto its server's real HTTP
API, and the caveats worth knowing before you rely on it.

## Plex

**Getting a token.** Plex has no concept of "API key" -- every request
authenticates as a specific Plex account via `X-Plex-Token`. The server
owner's admin token works for admin-only calls (`/accounts`, library-wide
`/library/all?guid=...` lookups), but **watch state is scoped to whichever
token makes the request** -- there is no "read user X's watched status" call
an admin token can use on someone else's behalf. So:

- Every person listed in `users[]` who should sync on a Plex server needs
  *their own* Plex token in `users[].accounts.<plex_server_key>`, not a
  shared admin token and not a username.
- The simplest way to get a personal token: sign into `https://plex.tv` as
  that user, then follow Plex's own "Finding an authentication token"
  instructions (Plex support article; search "Plex support find token" --
  deliberately not pasted here since Plex's own UI for this changes
  periodically and a stale walkthrough is worse than none).
- Plex Home users technically also support server-side token exchange via
  the plex.tv API (`/api/v2/home/users/{id}/switch`), which `PlexClient`
  does not implement -- if that's preferable for your setup it's a
  reasonable place to extend `list_users()`.

**What's actually implemented** (`watchable/providers/plex.py`):

- Auth: `X-Plex-Token` header, per-request (so a per-user token can be
  passed as `server_user_id` into `iter_watch_state`/`set_watch_state`
  without a separate client instance).
- Reading watch state: `GET /library/sections`, then
  `GET /library/sections/{key}/all?type=<1|4>&includeGuids=1` (1=movie,
  4=episode). `includeGuids=1` is what makes PMS attach the `Guid` array
  (`imdb://...`, `tmdb://...`, `tvdb://...`) to each item -- without it,
  cross-server matching has nothing to go on.
- Marking watched: `GET /:/scrobble?key=<ratingKey>&identifier=com.plexapp.plugins.library`.
- Marking unwatched: `GET /:/unscrobble` (same params).
- Setting in-progress position: `GET /:/progress?key=<ratingKey>&time=<ms>&state=stopped`.
- Cross-server lookup: `GET /library/all?type=<n>&guid=<scheme>://<value>`,
  a PMS-wide guid search. For episodes, the show is found this way first
  (`type=2`), then its episode list (`/library/metadata/{key}/allLeaves`)
  is scanned for the matching season/episode number.

**Known limitations:**
- Episodes' own `Guid` array is not used for matching (many episodes don't
  carry one at all) -- matching is always show-guid + season/episode number,
  so a show whose season/episode numbering genuinely differs between
  servers (rare, but happens with some anime) won't match correctly.
- `/library/all?guid=` performance depends on library size and Plex Media
  Server version; very large libraries may want this cached rather than
  queried per push (not currently done).

## Jellyfin

The more straightforward of the three -- one API key can read and write any
user's data, no per-user credential juggling.

**Getting an API key.** Jellyfin admin dashboard -> Advanced -> API Keys ->
add a new one. User IDs (for `users[].accounts`) are visible in the admin
dashboard's user list, or via `GET /Users` with that key.

**What's actually implemented** (`watchable/providers/jellyfin.py`):

- Auth: `X-Emby-Token: <api key>` header (kept from Jellyfin's Emby
  ancestry; still the correct header on current Jellyfin).
- Reading watch state: `GET /Users/{userId}/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=ProviderIds`.
  Each item DTO carries `UserData.Played` / `UserData.PlaybackPositionTicks`
  for the requesting user directly.
- Marking watched: `POST /Users/{userId}/PlayedItems/{itemId}`.
- Marking unwatched: `DELETE /Users/{userId}/PlayedItems/{itemId}`.
- Setting in-progress position: `POST /Users/{userId}/Items/{itemId}/UserData`
  with `{"PlaybackPositionTicks": ...}` (1 tick = 100ns, so 1ms = 10,000 ticks).
- Cross-server lookup: `GET /Items?Recursive=true&IncludeItemTypes=Movie&AnyProviderIdEquals=<scheme>.<value>`.

**Known limitations / verify against your version:**
- `AnyProviderIdEquals` is the one part of this integration most likely to
  need adjusting for an older Jellyfin server -- it's a relatively recent
  addition to the Items endpoint. Run `watchable test-connections` and a
  `--dry-run` sync after upgrading Jellyfin to confirm lookups still find
  matches; if they stop working, `EmbyClient`'s client-side scan approach
  (see below) is the fallback pattern to switch `JellyfinClient` to.
- Provider ID key casing (`Imdb` vs `IMDb` vs `imdb`) has varied across
  Jellyfin versions; `_extract_guids` lower-cases keys before matching to
  be resilient to this, but a version using a materially different scheme
  name would need a mapping added there.

## Emby

`EmbyClient` subclasses `JellyfinClient` -- the two APIs are close enough
(Jellyfin forked from Emby) that auth, `/Users`, `/Items`, and
`PlayedItems` all work identically. The one deliberate override:

- **Cross-server lookup does not use `AnyProviderIdEquals`.** That
  parameter is a Jellyfin-only addition; Emby doesn't support it. Instead,
  `EmbyClient._scan_for_guids` fetches all items of the candidate type with
  `Fields=ProviderIds` and filters client-side. This is heavier than a
  server-side filter, but fine for the library sizes a home-lab sync setup
  deals with. If you're running this against a very large Emby library and
  see lookups getting slow, that scan is the place to optimize (e.g. cache
  the provider-id -> item-id map for the duration of a sync run instead of
  re-fetching it per lookup).

**Getting an API key.** Emby dashboard -> Advanced -> API Keys, same idea
as Jellyfin.

## Adding a fourth provider

Implement `watchable.providers.base.MediaServerClient`'s five abstract
methods, register the class in `_REGISTRY` in
`watchable/providers/__init__.py`, and add its `type` value to the
`ServerConfig._check_credentials` validator in `watchable/config.py` (plus
whatever credential field it needs). Nothing in `watchable/sync.py` needs to
change -- see `docs/ARCHITECTURE.md#provider-interface`.
