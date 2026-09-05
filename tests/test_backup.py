from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from watchable.backup import create_backup, list_backups, purge_old_backups, restore_backup
from watchable.db import Database


def _row_count(db_path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "watchable.db"
    db = Database(path)
    db.resolve_or_create_item(media_type="movie", title="Shawshank", year=1994, guid_keys=("imdb:tt1",))
    db.close()
    return path


def test_create_backup_snapshots_current_data(db_path, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_path = create_backup(db_path, backup_dir)

    assert backup_path.exists()
    assert backup_path.parent == backup_dir
    assert _row_count(backup_path) == 1


def test_list_backups_sorted_newest_first(db_path, tmp_path):
    backup_dir = tmp_path / "backups"
    older = create_backup(db_path, backup_dir, when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = create_backup(db_path, backup_dir, when=datetime(2026, 1, 2, tzinfo=timezone.utc))

    backups = list_backups(backup_dir)

    assert [b.path for b in backups] == [newer, older]


def test_list_backups_ignores_unrelated_files(db_path, tmp_path):
    backup_dir = tmp_path / "backups"
    create_backup(db_path, backup_dir, when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    (backup_dir / "notes.txt").write_text("not a backup")

    backups = list_backups(backup_dir)

    assert len(backups) == 1


def test_list_backups_missing_dir_returns_empty(tmp_path):
    assert list_backups(tmp_path / "nonexistent") == []


def test_restore_backup_overwrites_target(db_path, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_path = create_backup(db_path, backup_dir)

    db = Database(db_path)
    db.resolve_or_create_item(media_type="movie", title="Fight Club", year=1999, guid_keys=("imdb:tt2",))
    db.close()
    assert _row_count(db_path) == 2

    restore_backup(backup_path, db_path)

    assert _row_count(db_path) == 1


def test_restore_missing_backup_raises(tmp_path, db_path):
    with pytest.raises(FileNotFoundError):
        restore_backup(tmp_path / "nope.db", db_path)


def test_purge_old_backups_deletes_only_expired(db_path, tmp_path):
    backup_dir = tmp_path / "backups"
    old = create_backup(db_path, backup_dir, when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    recent = create_backup(db_path, backup_dir, when=datetime(2026, 1, 20, tzinfo=timezone.utc))

    removed = purge_old_backups(backup_dir, keep_days=10, now=datetime(2026, 1, 21, tzinfo=timezone.utc))

    assert removed == [old]
    assert not old.exists()
    assert recent.exists()


def test_purge_old_backups_keeps_all_within_window(db_path, tmp_path):
    backup_dir = tmp_path / "backups"
    create_backup(db_path, backup_dir, when=datetime(2026, 1, 1, tzinfo=timezone.utc))

    removed = purge_old_backups(
        backup_dir, keep_days=365, now=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=1)
    )

    assert removed == []
