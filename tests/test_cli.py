from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from watchable.backup import create_backup
from watchable.cli import _initial_backup_deadline, _run_once, main
from watchable.config import (
    AppConfig,
    BackupConfig,
    DatabaseConfig,
    ServerConfig,
    SyncConfig,
    SyncPairConfig,
    UserConfig,
)

ONE_HOUR = 3600.0

# Reserved by IANA to never resolve (RFC 2606) -- a fast, offline-safe way
# to force a real connection failure without needing network access.
_UNREACHABLE_URL_A = "http://a.invalid"
_UNREACHABLE_URL_B = "http://b.invalid"


def _config(backup_dir) -> AppConfig:
    return AppConfig(
        backup=BackupConfig(dir=str(backup_dir)),
        servers=[
            ServerConfig(key="a", type="jellyfin", url="http://a", api_key="k"),
            ServerConfig(key="b", type="jellyfin", url="http://b", api_key="k"),
        ],
        sync=SyncConfig(pairs=[SyncPairConfig(name="p", source="a", target="b")]),
    )


def test_initial_backup_deadline_no_existing_backups_is_due_now(tmp_path):
    config = _config(tmp_path / "backups")
    before = time.monotonic()

    deadline = _initial_backup_deadline(config, ONE_HOUR)

    assert deadline <= before + 1


def test_initial_backup_deadline_old_backup_is_due_now(tmp_path):
    backup_dir = tmp_path / "backups"
    create_backup(tmp_path / "watchable.db", backup_dir, when=datetime.now(timezone.utc) - timedelta(hours=2))
    config = _config(backup_dir)
    before = time.monotonic()

    deadline = _initial_backup_deadline(config, ONE_HOUR)

    assert deadline <= before + 1


def test_initial_backup_deadline_recent_backup_waits_out_the_remainder(tmp_path):
    # A restart 10 minutes after the last backup, on a 1-hour cadence,
    # should wait ~50 minutes for the next one -- not back it up again
    # immediately just because the process restarted.
    backup_dir = tmp_path / "backups"
    create_backup(tmp_path / "watchable.db", backup_dir, when=datetime.now(timezone.utc) - timedelta(minutes=10))
    config = _config(backup_dir)

    deadline = _initial_backup_deadline(config, ONE_HOUR)

    remaining = deadline - time.monotonic()
    assert 2900 < remaining < 3100


def _config_with_unreachable_servers(tmp_path) -> AppConfig:
    return AppConfig(
        database=DatabaseConfig(path=str(tmp_path / "watchable.db")),
        backup=BackupConfig(dir=str(tmp_path / "backups")),
        servers=[
            ServerConfig(key="a", type="jellyfin", url=_UNREACHABLE_URL_A, api_key="k"),
            ServerConfig(key="b", type="jellyfin", url=_UNREACHABLE_URL_B, api_key="k"),
        ],
        users=[UserConfig(name="alex", accounts={"a": "u1", "b": "u2"})],
        sync=SyncConfig(pairs=[SyncPairConfig(name="p", source="a", target="b")]),
    )


def test_run_once_returns_stats_instead_of_exiting_on_errors(tmp_path):
    # A pull failure (server unreachable) must not kill a long-lived `run`
    # loop -- _run_once itself no longer decides to exit; it just reports
    # what happened via the returned stats. If this raised SystemExit,
    # this call would never return.
    config = _config_with_unreachable_servers(tmp_path)

    stats = _run_once(config, dry_run=False)

    assert stats.errors > 0


def test_sync_cmd_still_exits_nonzero_on_errors(tmp_path):
    # The one-shot `sync` command (used from cron/systemd) still needs a
    # nonzero exit code on failure -- only `run`'s loop should swallow it.
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
database:
  path: {tmp_path / "watchable.db"}
servers:
  a:
    type: jellyfin
    url: {_UNREACHABLE_URL_A}
    api_key: k
  b:
    type: jellyfin
    url: {_UNREACHABLE_URL_B}
    api_key: k
users:
  - name: alex
    accounts:
      a: u1
      b: u2
sync:
  pairs:
    - name: p
      source: a
      target: b
""")

    result = CliRunner().invoke(main, ["sync", str(config_path)])

    assert result.exit_code == 1
