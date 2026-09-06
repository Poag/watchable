"""Local SQLite state store.

watchable never syncs one media server directly to another. Every run pulls
watch state from each configured server into this local database first, then
reconciles and pushes from here out to targets. That indirection is what
lets a bidirectional pair merge changes from both sides instead of one
side's poll simply overwriting the other's, and it gives every sync action
somewhere durable to be diffed against and logged.

Schema (see ``docs/ARCHITECTURE.md`` for the full rationale):

- ``items`` / ``item_guids``: canonical library items, matched by external
  ids (imdb/tmdb/tvdb) or by show-guid + season/episode for TV episodes.
- ``user_server_accounts``: maps a config-level person to their account on
  each server.
- ``watch_state``: one row per (item, user, server) -- the last watch state
  *observed on that server* the last time it was polled.
- ``sync_log``: an audit trail of every push watchable has made.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from watchable.matching import parse_guid_key
from watchable.providers.base import EPISODE, WatchStateRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER
);

CREATE TABLE IF NOT EXISTS item_guids (
    guid_key TEXT PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_item_guids_item_id ON item_guids(item_id);

CREATE TABLE IF NOT EXISTS user_server_accounts (
    user_name TEXT NOT NULL,
    server_key TEXT NOT NULL,
    server_user_id TEXT NOT NULL,
    PRIMARY KEY (user_name, server_key)
);

CREATE TABLE IF NOT EXISTS watch_state (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL,
    server_key TEXT NOT NULL,
    server_item_id TEXT NOT NULL,
    played INTEGER NOT NULL,
    view_offset_ms INTEGER NOT NULL,
    runtime_ms INTEGER,
    last_played_at TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (item_id, user_name, server_key)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    pair_name TEXT NOT NULL,
    item_id INTEGER,
    title TEXT,
    user_name TEXT NOT NULL,
    source_server TEXT NOT NULL,
    target_server TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_log_ts ON sync_log(ts);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StoredWatchState:
    item_id: int
    server_key: str
    server_item_id: str
    played: bool
    view_offset_ms: int
    runtime_ms: int | None
    last_played_at: str | None
    observed_at: str


class Database:
    """Thin wrapper around a single sqlite3 connection.

    Not thread-safe by design -- watchable runs one sync pass at a time in a
    single process/thread, so a plain connection with WAL journaling is
    simpler and more debuggable than a pool.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- user accounts -----------------------------------------------------

    def upsert_user_account(self, user_name: str, server_key: str, server_user_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO user_server_accounts (user_name, server_key, server_user_id)
                VALUES (?, ?, ?)
                ON CONFLICT (user_name, server_key)
                DO UPDATE SET server_user_id = excluded.server_user_id
                """,
                (user_name, server_key, server_user_id),
            )

    # -- items / matching ----------------------------------------------------

    def resolve_or_create_item(
        self, *, media_type: str, title: str, year: int | None, guid_keys: tuple[str, ...]
    ) -> int:
        """Find the canonical item these guid_keys belong to, creating one if needed.

        If guid_keys span multiple existing items (e.g. this server only had
        a tmdb id, an earlier server only had an imdb id, and both turn out
        to describe the same item), the items are merged into the
        lowest-numbered id so the two servers' history reconciles as one item
        going forward.
        """
        if not guid_keys:
            raise ValueError("resolve_or_create_item requires at least one guid_key")

        with self.transaction() as conn:
            found_item_ids: set[int] = set()
            for key in guid_keys:
                row = conn.execute(
                    "SELECT item_id FROM item_guids WHERE guid_key = ?", (key,)
                ).fetchone()
                if row is not None:
                    found_item_ids.add(row["item_id"])

            if not found_item_ids:
                cur = conn.execute(
                    "INSERT INTO items (media_type, title, year) VALUES (?, ?, ?)",
                    (media_type, title, year),
                )
                assert cur.lastrowid is not None
                item_id = cur.lastrowid
            elif len(found_item_ids) == 1:
                item_id = next(iter(found_item_ids))
            else:
                # Merge: keep the lowest id, repoint everything else at it.
                item_id = min(found_item_ids)
                for stale_id in found_item_ids - {item_id}:
                    conn.execute(
                        "UPDATE item_guids SET item_id = ? WHERE item_id = ?",
                        (item_id, stale_id),
                    )
                    conn.execute(
                        """
                        UPDATE OR IGNORE watch_state SET item_id = ? WHERE item_id = ?
                        """,
                        (item_id, stale_id),
                    )
                    conn.execute("DELETE FROM watch_state WHERE item_id = ?", (stale_id,))
                    conn.execute("DELETE FROM items WHERE id = ?", (stale_id,))

            for key in guid_keys:
                conn.execute(
                    "INSERT OR IGNORE INTO item_guids (guid_key, item_id) VALUES (?, ?)",
                    (key, item_id),
                )

            # Keep the stored title current rather than freezing whatever
            # was seen the first time this item was created -- a provider
            # can supply a better title later (e.g. this codebase learning
            # to send an episode's show title instead of its own), and an
            # item shouldn't be stuck with a stale or placeholder one
            # forever. "Unknown" is providers' own fallback for genuinely
            # missing data, so don't let it clobber a real title already stored.
            if title and title != "Unknown":
                conn.execute("UPDATE items SET title = ? WHERE id = ?", (title, item_id))

            return item_id

    # -- watch state ---------------------------------------------------------

    def record_watch_state(
        self, *, item_id: int, user_name: str, server_key: str, record: WatchStateRecord
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO watch_state (
                    item_id, user_name, server_key, server_item_id,
                    played, view_offset_ms, runtime_ms, last_played_at, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (item_id, user_name, server_key) DO UPDATE SET
                    server_item_id = excluded.server_item_id,
                    played = excluded.played,
                    view_offset_ms = excluded.view_offset_ms,
                    runtime_ms = excluded.runtime_ms,
                    last_played_at = excluded.last_played_at,
                    observed_at = excluded.observed_at
                """,
                (
                    item_id,
                    user_name,
                    server_key,
                    record.server_item_id,
                    int(record.played),
                    record.view_offset_ms,
                    record.runtime_ms,
                    record.last_played_at.isoformat() if record.last_played_at else None,
                    _utcnow_iso(),
                ),
            )

    def get_watch_state(
        self, *, item_id: int, user_name: str, server_key: str
    ) -> StoredWatchState | None:
        row = self.conn.execute(
            """
            SELECT item_id, server_key, server_item_id, played, view_offset_ms,
                   runtime_ms, last_played_at, observed_at
            FROM watch_state
            WHERE item_id = ? AND user_name = ? AND server_key = ?
            """,
            (item_id, user_name, server_key),
        ).fetchone()
        if row is None:
            return None
        return StoredWatchState(
            item_id=row["item_id"],
            server_key=row["server_key"],
            server_item_id=row["server_item_id"],
            played=bool(row["played"]),
            view_offset_ms=row["view_offset_ms"],
            runtime_ms=row["runtime_ms"],
            last_played_at=row["last_played_at"],
            observed_at=row["observed_at"],
        )

    def iter_item_ids_for_user(self, user_name: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT item_id FROM watch_state WHERE user_name = ?", (user_name,)
        ).fetchall()
        return [row["item_id"] for row in rows]

    def get_item_title(self, item_id: int) -> str:
        row = self.conn.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()
        return row["title"] if row else f"item#{item_id}"

    def get_item_media_type(self, item_id: int) -> str:
        row = self.conn.execute("SELECT media_type FROM items WHERE id = ?", (item_id,)).fetchone()
        return row["media_type"] if row else "movie"

    def get_guid_keys_for_item(self, item_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT guid_key FROM item_guids WHERE item_id = ?", (item_id,)
        ).fetchall()
        return [row["guid_key"] for row in rows]

    def get_item_display_name(self, item_id: int) -> str:
        """Human-readable label for logs: "Show S01E02" for episodes, else the title.

        For episodes, ``title`` (see resolve_or_create_item) is the show's own
        title, not the individual episode's -- season/episode numbers are
        recovered from a stored guid key instead of a separate column.
        """
        title = self.get_item_title(item_id)
        if self.get_item_media_type(item_id) != EPISODE:
            return title
        for key in self.get_guid_keys_for_item(item_id):
            _, _, season, episode_number = parse_guid_key(key)
            if season is not None and episode_number is not None:
                return f"{title} S{season:02d}E{episode_number:02d}"
        return title

    # -- audit log -------------------------------------------------------

    def log_sync_action(
        self,
        *,
        pair_name: str,
        item_id: int | None,
        title: str | None,
        user_name: str,
        source_server: str,
        target_server: str,
        action: str,
        detail: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sync_log (
                    ts, pair_name, item_id, title, user_name,
                    source_server, target_server, action, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow_iso(),
                    pair_name,
                    item_id,
                    title,
                    user_name,
                    source_server,
                    target_server,
                    action,
                    detail,
                ),
            )

    def copy_watch_state(
        self,
        *,
        item_id: int,
        user_name: str,
        from_server_key: str,
        to_server_key: str,
        to_server_item_id: str,
    ) -> None:
        """After a successful push, mirror the source's row onto the target.

        This keeps the local DB consistent with what we just wrote to the
        target server, without needing another network round-trip to
        re-pull it before the next sync pass.
        """
        source = self.get_watch_state(item_id=item_id, user_name=user_name, server_key=from_server_key)
        if source is None:
            return
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO watch_state (
                    item_id, user_name, server_key, server_item_id,
                    played, view_offset_ms, runtime_ms, last_played_at, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (item_id, user_name, server_key) DO UPDATE SET
                    server_item_id = excluded.server_item_id,
                    played = excluded.played,
                    view_offset_ms = excluded.view_offset_ms,
                    runtime_ms = excluded.runtime_ms,
                    last_played_at = excluded.last_played_at,
                    observed_at = excluded.observed_at
                """,
                (
                    item_id,
                    user_name,
                    to_server_key,
                    to_server_item_id,
                    int(source.played),
                    source.view_offset_ms,
                    source.runtime_ms,
                    source.last_played_at,
                    _utcnow_iso(),
                ),
            )

    def recent_log(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
