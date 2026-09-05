"""The sync engine: pull every server's watch state into the local database,
then reconcile and push according to each configured sync pair.

Two phases per run, always in this order:

1. **Pull** -- for every server referenced by any pair, for every configured
   user with an account on it, fetch that user's current watch state and
   record it in the local DB. Nothing is written back to any server here.
2. **Reconcile + push** -- for every pair, for every user present on both
   sides, compare the source's and (for bidirectional pairs) target's
   locally-stored state and push whichever side should change.

Keeping pull and push as separate phases means a run's decisions are always
based on one consistent snapshot, rather than a push mutating a state that
a later step in the same run then reads back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from watchable.config import AppConfig, ConflictStrategy, Direction, SyncPairConfig
from watchable.db import Database, StoredWatchState
from watchable.matching import build_guid_keys, guids_from_stored_keys
from watchable.providers.base import MediaServerClient, WatchStateRecord

logger = logging.getLogger("watchable.sync")

#: Sub-30-second view-offset differences are treated as noise (seeking,
#: buffering, a client rounding differently) rather than a real state change.
VIEW_OFFSET_TOLERANCE_MS = 30_000


@dataclass
class SyncStats:
    pulled: int = 0
    pushed: int = 0
    dry_run_pushes: int = 0
    skipped_no_match: int = 0
    unchanged: int = 0
    errors: int = 0
    log: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.log.append(message)
        logger.info(message)


def _states_differ(a: StoredWatchState, b: StoredWatchState) -> bool:
    if a.played != b.played:
        return True
    if not a.played and not b.played:
        return abs(a.view_offset_ms - b.view_offset_ms) > VIEW_OFFSET_TOLERANCE_MS
    return False


class SyncEngine:
    def __init__(self, config: AppConfig, db: Database, clients: dict[str, MediaServerClient]):
        self.config = config
        self.db = db
        self.clients = clients
        for user in config.users:
            for server_key, server_user_id in user.accounts.items():
                self.db.upsert_user_account(user.name, server_key, server_user_id)

    def run(self, *, dry_run: bool = False) -> SyncStats:
        stats = SyncStats()
        self._pull_all(stats)
        for pair in self.config.sync.pairs:
            stats.note(
                f"Reconciling pair {pair.name!r} "
                f"({pair.source} -> {pair.target}, {pair.direction.value})"
            )
            self._reconcile_pair(pair, stats, dry_run=dry_run)
        return stats

    # -- pull ----------------------------------------------------------------

    def _pull_all(self, stats: SyncStats) -> None:
        media_types_needed: dict[str, set[str]] = {}
        for pair in self.config.sync.pairs:
            for key in (pair.source, pair.target):
                media_types_needed.setdefault(key, set()).update(pair.media_types)

        for server_key, media_types in media_types_needed.items():
            client = self.clients[server_key]
            for user in self.config.users:
                server_user_id = user.accounts.get(server_key)
                if server_user_id is None:
                    continue
                try:
                    records = list(client.iter_watch_state(server_user_id, media_types=tuple(media_types)))
                except Exception:
                    logger.exception(
                        "Failed pulling watch state from %s for user %s", server_key, user.name
                    )
                    stats.errors += 1
                    continue
                for record in records:
                    self._ingest_record(server_key, user.name, record, stats)

    def _ingest_record(
        self, server_key: str, user_name: str, record: WatchStateRecord, stats: SyncStats
    ) -> None:
        guid_keys = build_guid_keys(record.media_type, record.item_guids, record.episode)
        if not guid_keys:
            stats.skipped_no_match += 1
            logger.debug("Skipping %r from %s: no external ids to match on", record.title, server_key)
            return
        item_id = self.db.resolve_or_create_item(
            media_type=record.media_type, title=record.title, year=None, guid_keys=guid_keys
        )
        self.db.record_watch_state(item_id=item_id, user_name=user_name, server_key=server_key, record=record)
        stats.pulled += 1

    # -- reconcile + push ------------------------------------------------------

    def _reconcile_pair(self, pair: SyncPairConfig, stats: SyncStats, *, dry_run: bool) -> None:
        source_client = self.clients[pair.source]
        target_client = self.clients[pair.target]
        strategy = pair.conflict_strategy or self.config.sync.conflict_strategy

        for user in self.config.users:
            source_user_id = user.accounts.get(pair.source)
            target_user_id = user.accounts.get(pair.target)
            if source_user_id is None or target_user_id is None:
                continue  # this user isn't on both servers in this pair

            for item_id in self.db.iter_item_ids_for_user(user.name):
                source_state = self.db.get_watch_state(
                    item_id=item_id, user_name=user.name, server_key=pair.source
                )
                target_state = self.db.get_watch_state(
                    item_id=item_id, user_name=user.name, server_key=pair.target
                )
                if source_state is None and target_state is None:
                    continue
                if pair.direction == Direction.ONE_WAY and source_state is None:
                    continue  # one-way never flows from target back to source

                self._reconcile_item(
                    pair=pair,
                    strategy=strategy,
                    user_name=user.name,
                    source_user_id=source_user_id,
                    target_user_id=target_user_id,
                    source_client=source_client,
                    target_client=target_client,
                    item_id=item_id,
                    source_state=source_state,
                    target_state=target_state,
                    stats=stats,
                    dry_run=dry_run,
                )

    def _reconcile_item(
        self,
        *,
        pair: SyncPairConfig,
        strategy: ConflictStrategy,
        user_name: str,
        source_user_id: str,
        target_user_id: str,
        source_client: MediaServerClient,
        target_client: MediaServerClient,
        item_id: int,
        source_state: StoredWatchState | None,
        target_state: StoredWatchState | None,
        stats: SyncStats,
        dry_run: bool,
    ) -> None:
        title = self.db.get_item_title(item_id)

        if pair.direction == Direction.ONE_WAY:
            assert source_state is not None
            if target_state is not None and not _states_differ(source_state, target_state):
                stats.unchanged += 1
                return
            self._push(
                pair=pair, stats=stats, dry_run=dry_run, item_id=item_id, title=title, user_name=user_name,
                from_state=source_state, from_server_key=pair.source,
                to_client=target_client, to_server_key=pair.target, to_user_id=target_user_id,
                to_state=target_state,
            )
            return

        # Bidirectional: whichever side is missing state gets the other side's.
        if source_state is None:
            assert target_state is not None
            self._push(
                pair=pair, stats=stats, dry_run=dry_run, item_id=item_id, title=title, user_name=user_name,
                from_state=target_state, from_server_key=pair.target,
                to_client=source_client, to_server_key=pair.source, to_user_id=source_user_id,
                to_state=None,
            )
            return
        if target_state is None:
            self._push(
                pair=pair, stats=stats, dry_run=dry_run, item_id=item_id, title=title, user_name=user_name,
                from_state=source_state, from_server_key=pair.source,
                to_client=target_client, to_server_key=pair.target, to_user_id=target_user_id,
                to_state=None,
            )
            return

        if not _states_differ(source_state, target_state):
            stats.unchanged += 1
            return

        (
            winner_key, winner_state, winner_state_is_target,
        ) = self._pick_winner(strategy, source_state, target_state)

        if winner_state_is_target:
            self._push(
                pair=pair, stats=stats, dry_run=dry_run, item_id=item_id, title=title, user_name=user_name,
                from_state=target_state, from_server_key=pair.target,
                to_client=source_client, to_server_key=pair.source, to_user_id=source_user_id,
                to_state=source_state,
            )
        else:
            self._push(
                pair=pair, stats=stats, dry_run=dry_run, item_id=item_id, title=title, user_name=user_name,
                from_state=source_state, from_server_key=pair.source,
                to_client=target_client, to_server_key=pair.target, to_user_id=target_user_id,
                to_state=target_state,
            )

    @staticmethod
    def _pick_winner(
        strategy: ConflictStrategy, source_state: StoredWatchState, target_state: StoredWatchState
    ) -> tuple[str, StoredWatchState, bool]:
        """Return (winner_server_key placeholder unused, winner_state, is_target)."""
        if strategy == ConflictStrategy.SOURCE_WINS:
            return "source", source_state, False
        if strategy == ConflictStrategy.TARGET_WINS:
            return "target", target_state, True
        if strategy == ConflictStrategy.LATEST:
            source_ts = source_state.last_played_at or source_state.observed_at
            target_ts = target_state.last_played_at or target_state.observed_at
            if target_ts > source_ts:
                return "target", target_state, True
            return "source", source_state, False
        # most_watched (default): played beats unplayed, then higher offset wins
        source_score = (int(source_state.played), source_state.view_offset_ms)
        target_score = (int(target_state.played), target_state.view_offset_ms)
        if target_score > source_score:
            return "target", target_state, True
        return "source", source_state, False

    def _push(
        self,
        *,
        pair: SyncPairConfig,
        stats: SyncStats,
        dry_run: bool,
        item_id: int,
        title: str,
        user_name: str,
        from_state: StoredWatchState,
        from_server_key: str,
        to_client: MediaServerClient,
        to_server_key: str,
        to_user_id: str,
        to_state: StoredWatchState | None,
    ) -> None:
        to_item_id: str | None
        if to_state is not None:
            to_item_id = to_state.server_item_id
        else:
            media_type = self.db.get_item_media_type(item_id)
            guid_keys = self.db.get_guid_keys_for_item(item_id)
            guids, episode = guids_from_stored_keys(guid_keys)
            try:
                to_item_id = to_client.find_item_by_guids(media_type, guids, episode)
            except Exception:
                logger.exception("Lookup failed on %s for %r", to_server_key, title)
                stats.errors += 1
                return
            if to_item_id is None:
                self.db.log_sync_action(
                    pair_name=pair.name, item_id=item_id, title=title, user_name=user_name,
                    source_server=from_server_key, target_server=to_server_key,
                    action="skip", detail="item not found in target library",
                )
                stats.skipped_no_match += 1
                return

        detail = f"played={from_state.played} offset_ms={from_state.view_offset_ms}"

        if dry_run:
            self.db.log_sync_action(
                pair_name=pair.name, item_id=item_id, title=title, user_name=user_name,
                source_server=from_server_key, target_server=to_server_key,
                action="dry_run_push", detail=detail,
            )
            stats.dry_run_pushes += 1
            stats.note(f"[dry-run] would push {title!r}: {from_server_key} -> {to_server_key} ({detail})")
            return

        try:
            to_client.set_watch_state(
                to_user_id,
                to_item_id,
                played=from_state.played,
                view_offset_ms=from_state.view_offset_ms,
                runtime_ms=from_state.runtime_ms,
            )
        except Exception:
            logger.exception("Push failed: %s -> %s for %r", from_server_key, to_server_key, title)
            stats.errors += 1
            return

        self.db.copy_watch_state(
            item_id=item_id, user_name=user_name,
            from_server_key=from_server_key, to_server_key=to_server_key, to_server_item_id=to_item_id,
        )
        self.db.log_sync_action(
            pair_name=pair.name, item_id=item_id, title=title, user_name=user_name,
            source_server=from_server_key, target_server=to_server_key,
            action="push", detail=detail,
        )
        stats.pushed += 1
        stats.note(f"Pushed {title!r}: {from_server_key} -> {to_server_key} ({detail})")
