# Contributing

## Setup

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running checks

```
pytest              # unit tests (no network, no real media servers needed)
ruff check src tests
mypy src
```

All three run against the fake in-memory provider clients in
`tests/test_sync.py` and mocked HTTP (via `responses`) in
`tests/test_providers_*.py` -- there's no need for a real Plex/Jellyfin/Emby
instance to develop against. If you're changing provider client behavior,
`watchable test-connections config.yaml` and `watchable sync config.yaml
--dry-run` against a real (ideally test) server are the way to confirm it
against the genuine API before relying on the mocked tests alone.

## Project layout

```
src/watchable/
  config.py       # YAML loading + validation (pydantic models)
  db.py           # SQLite schema + queries
  matching.py     # cross-server item identity (guid keys)
  providers/      # one client per media server, common MediaServerClient interface
  sync.py         # pull -> reconcile -> push engine
  app.py          # wires config -> clients + database
  cli.py          # `watchable` command-line entry point
tests/            # pytest; fakes/mocks only, no live servers required
docs/             # architecture, configuration reference, provider notes
docker/           # container packaging
icon/             # project icon source + rendered sizes
```

See `docs/ARCHITECTURE.md` for how the pieces fit together and why the
design pulls state into a local database rather than syncing servers
directly.

## Style

- Formatting/lint: `ruff` (config in `pyproject.toml`). Run `ruff check --fix`
  before committing.
- Type hints throughout; `mypy src` should report no issues.
- Prefer adding a test alongside any behavior change over a purely manual
  check -- `tests/test_sync.py`'s `FakeClient` is the quickest way to cover
  new reconciliation logic without touching a real server.

## Adding a media server

See "Adding a fourth provider" at the end of `docs/PROVIDERS.md`.
