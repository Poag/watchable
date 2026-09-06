<p align="center">
  <img src="icon/watchable-256.png" width="128" height="128" alt="watchable icon">
</p>

<h1 align="center">watchable</h1>

<p align="center">
  Sync watch state between Plex, Jellyfin, and Emby through a local database,
  configured entirely in YAML.
</p>

---

Watch something on one media server, and watchable can carry that watched /
in-progress state to the others -- one-way (server A always overwrites
server B) or bidirectional (either side can change, with a configurable
rule for who wins when both did). Nothing syncs server-to-server directly:
every run pulls current watch state from each configured server into a
local SQLite database first, reconciles it there, then pushes out whatever
changed. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for why.

## Features

- **Plex, Jellyfin, and Emby**, matched to each other by external ids
  (IMDb/TMDb/TVDb) so it doesn't matter that each server assigns its own
  unrelated internal IDs to the same movie or episode.
- **One-way or bidirectional** sync, set per pair of servers.
- **Configurable conflict resolution** for bidirectional pairs:
  most-progressed-wins (default), most-recently-played-wins, or a fixed
  side always wins.
- **Everything in one YAML file** -- servers, per-person account mapping,
  and sync pairs. No database setup, no web UI to click through.
- **A local audit trail** (`watchable log`) of every push it's made, and
  `--dry-run` to see what a sync *would* do before it does it.
- **Database backups** (`watchable backup create/list/restore/purge`), on
  their own configurable schedule if you use `watchable run`, with old
  backups purged automatically.

## Quickstart

```
pip install -e .
cp config.example.yaml config.yaml
$EDITOR config.yaml   # fill in your servers, users, and sync pairs

watchable validate config.yaml         # check the config parses and is internally consistent
watchable test-connections config.yaml # check every server is reachable
watchable sync config.yaml --dry-run   # see what would happen
watchable sync config.yaml             # do it
watchable log config.yaml              # see what it did
```

Full configuration reference: [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).
Per-server API details and caveats (Plex in particular works differently
enough from Jellyfin/Emby to matter): [`docs/PROVIDERS.md`](docs/PROVIDERS.md).

### Running continuously

`watchable sync` runs once and exits -- a cron job or systemd timer calling
it on a schedule is the simplest way to run it continuously. If you'd
rather run it as a long-lived process instead (e.g. the provided Docker
image), set `sync.schedule.interval_minutes` in your config and use
`watchable run` in place of `watchable sync`.

### Backups

```
watchable backup create config.yaml    # snapshot the database now
watchable backup list config.yaml      # see what's available
watchable backup restore config.yaml <file>   # roll back to a snapshot
watchable backup purge config.yaml     # delete backups older than backup.keep_days
```

Set `backup.interval_hours` in your config to have `watchable run` also
take backups on its own schedule (independent of the sync interval),
purging anything older than `backup.keep_days` afterwards. See
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md#backup).

### Docker

```
docker build -t watchable -f docker/Dockerfile .
docker run -e PUID=1000 -e PGID=1000 \
  -v ./config.yaml:/config/config.yaml:ro -v ./data:/data watchable
```

The container starts as root and drops to the built-in `watchable` user
remapped to `PUID`/`PGID` (default 1000:1000) before running anything --
set them to match whoever owns `./data` on the host if that's not 1000:1000.

Or see [`docker/docker-compose.example.yml`](docker/docker-compose.example.yml).
Every push to `main` publishes a multi-arch (`linux/amd64` + `linux/arm64`)
image to `ghcr.io/poag/watchable`.

For [Dockhand](https://dockhand.pro) or any other environment-variable-driven
deployment, see [`docker/docker-compose.dockhand.yml`](docker/docker-compose.dockhand.yml)
and [`docker/.env.dockhand.example`](docker/.env.dockhand.example) -- it pulls
the published GHCR image instead of building locally, and every
deployment-specific setting (image tag, paths, restart policy, the secrets
`config.yaml` references) comes from an environment variable.

## Example configuration

A one-way mirror plus a bidirectional pair, from `config.example.yaml`:

```yaml
sync:
  conflict_strategy: most_watched
  pairs:
    - name: plex-to-jellyfin
      source: plex_main
      target: jellyfin_main
      direction: one-way

    - name: jellyfin-emby-mirror
      source: jellyfin_main
      target: emby_main
      direction: bidirectional
      conflict_strategy: latest
```

## How it works, briefly

1. **Pull**: for every server and every configured person with an account
   on it, fetch their current watch state into the local database.
2. **Reconcile + push**: for every sync pair, compare each item's locally
   stored state on both sides and push whichever side needs to change --
   respecting that pair's direction and (for bidirectional pairs) conflict
   strategy.

Full details, including how items are matched across servers and the
database schema: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Development

```
pip install -e ".[dev]"
pytest
ruff check src tests
mypy src
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

[MIT](LICENSE)
