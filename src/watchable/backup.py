"""Database backup, restore, and retention.

A backup is a point-in-time copy of the whole SQLite database file, taken
via sqlite3's own backup API rather than a plain file copy so a live
WAL-mode connection mid-write can never produce a torn/partial snapshot.
Restoring one puts ``items``/``item_guids``, ``watch_state``, and
``sync_log`` all back to that moment together -- there's no separate
"restore just the watch state" path, since a partial restore could leave
``watch_state`` rows pointing at ``item_id``s that mean something different
than they did when the backup was taken.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_FILENAME_PATTERN = re.compile(r"^watchable-(\d{8}T\d{6}Z)\.db$")


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: datetime
    size_bytes: int


def _backup_filename(when: datetime) -> str:
    return f"watchable-{when.strftime(_TIMESTAMP_FORMAT)}.db"


def _parse_backup_filename(path: Path) -> datetime | None:
    match = _FILENAME_PATTERN.match(path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1), _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def _sqlite_copy(source_path: Path, dest_path: Path) -> None:
    source = sqlite3.connect(str(source_path))
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def create_backup(db_path: Path, backup_dir: Path, *, when: datetime | None = None) -> Path:
    """Snapshot ``db_path`` into ``backup_dir``, returning the new backup's path."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest_path = backup_dir / _backup_filename(when or datetime.now(timezone.utc))
    _sqlite_copy(db_path, dest_path)
    return dest_path


def list_backups(backup_dir: Path) -> list[BackupInfo]:
    """List watchable's own backups in ``backup_dir``, newest first."""
    if not backup_dir.exists():
        return []
    backups = []
    for path in backup_dir.glob("watchable-*.db"):
        created_at = _parse_backup_filename(path)
        if created_at is None:
            continue  # not a filename watchable itself produced -- leave it alone
        backups.append(BackupInfo(path=path, created_at=created_at, size_bytes=path.stat().st_size))
    backups.sort(key=lambda b: b.created_at, reverse=True)
    return backups


def restore_backup(backup_path: Path, db_path: Path) -> None:
    """Overwrite ``db_path`` with the contents of ``backup_path``.

    Any leftover ``-wal``/``-shm`` sidecar files next to ``db_path`` are
    removed too -- otherwise stale WAL frames from before the restore could
    be replayed onto the freshly-restored file the next time it's opened.
    Callers are responsible for making sure no sync is running concurrently.
    """
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    _sqlite_copy(backup_path, db_path)

    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def purge_old_backups(backup_dir: Path, keep_days: float, *, now: datetime | None = None) -> list[Path]:
    """Delete backups older than ``keep_days``, returning the paths removed."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    removed = []
    for backup in list_backups(backup_dir):
        if backup.created_at < cutoff:
            backup.path.unlink()
            removed.append(backup.path)
    return removed
