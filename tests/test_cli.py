from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from watchable.backup import create_backup
from watchable.cli import _initial_backup_deadline
from watchable.config import AppConfig, BackupConfig, ServerConfig, SyncConfig, SyncPairConfig

ONE_HOUR = 3600.0


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
