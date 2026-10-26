"""Sync engine tests using in-memory fake provider clients.

These exercise watchable.sync.SyncEngine against fakes rather than real Plex/
Jellyfin/Emby servers, so they run the reconciliation and conflict-resolution
logic -- the part that's actually watchable's own -- without needing network
access or real media servers.
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

from watchable.config import (
    AppConfig,
    ConflictStrategy,
    DatabaseConfig,
    Direction,
    ServerConfig,
    SyncConfig,
    SyncPairConfig,
    UserConfig,
)
from watchable.providers.base import (
    EPISODE,
    MOVIE,
    EpisodeInfo,
    GuidSet,
    MediaServerClient,
    ProviderError,
    WatchStateRecord,
)
from watchable.sync import SyncEngine


class FakeClient(MediaServerClient):
    server_type = "fake"

    def __init__(self, name: str):
        super().__init__(name=name, base_url="http://fake.invalid")
        self.library: dict[str, tuple[str, GuidSet, EpisodeInfo | None]] = {}
        self.watch_state: dict[tuple[str, str], dict] = {}
        self.push_calls: list[tuple] = []

    def add_item(self, server_item_id, media_type, guids, episode=None):
        self.library[server_item_id] = (media_type, guids, episode)

    def set_state(self, user_id, server_item_id, *, played, view_offset_ms=0, runtime_ms=7_200_000, last_played_at=None):
        self.watch_state[(user_id, server_item_id)] = dict(
            played=played, view_offset_ms=view_offset_ms, runtime_ms=runtime_ms, last_played_at=last_played_at
        )

    def test_connection(self) -> None:
        pass

    def list_users(self):
        return []

    def iter_watch_state(self, server_user_id, media_types=()):
        media_types = tuple(media_types)
        for server_item_id, (media_type, guids, episode) in self.library.items():
            if media_types and media_type not in media_types:
                continue
            state = self.watch_state.get((server_user_id, server_item_id))
            if state is None:
                continue
            yield WatchStateRecord(
                server_item_id=server_item_id,
                media_type=media_type,
                title=f"item-{server_item_id}",
                item_guids=guids,
                episode=episode,
                server_user_id=server_user_id,
                played=state["played"],
                view_offset_ms=state["view_offset_ms"],
                runtime_ms=state["runtime_ms"],
                last_played_at=state["last_played_at"],
            )

    def set_watch_state(self, server_user_id, server_item_id, *, played, view_offset_ms, runtime_ms):
        self.push_calls.append((server_user_id, server_item_id, played, view_offset_ms))
        self.set_state(server_user_id, server_item_id, played=played, view_offset_ms=view_offset_ms, runtime_ms=runtime_ms)

    def find_item_by_guids(self, media_type, item_guids, episode=None):
        for server_item_id, (m_type, guids, ep) in self.library.items():
            if m_type != media_type:
                continue
            if media_type == EPISODE:
                if (
                    ep is not None
                    and episode is not None
                    and set(ep.show_guids.canonical_keys()) & set(episode.show_guids.canonical_keys())
                    and ep.season_number == episode.season_number
                    and ep.episode_number == episode.episode_number
                ):
                    return server_item_id
            elif set(guids.canonical_keys()) & set(item_guids.canonical_keys()):
                return server_item_id
        return None


class FlakyClient(FakeClient):
    """Like FakeClient, but set_watch_state can be told to fail for specific ids."""

    def __init__(self, name: str):
        super().__init__(name)
        self.fail_item_ids: set[str] = set()

    def set_watch_state(self, server_user_id, server_item_id, *, played, view_offset_ms, runtime_ms):
        if server_item_id in self.fail_item_ids:
            response = requests.Response()
            response.status_code = 401
            raise ProviderError("401 for stale item") from requests.exceptions.HTTPError(response=response)
        super().set_watch_state(
            server_user_id, server_item_id, played=played, view_offset_ms=view_offset_ms, runtime_ms=runtime_ms
        )


def make_config(pairs: list[SyncPairConfig], strategy: ConflictStrategy = ConflictStrategy.MOST_WATCHED) -> AppConfig:
    return AppConfig(
        database=DatabaseConfig(path="unused"),
        servers=[
            ServerConfig(key="src", type="jellyfin", url="http://src", api_key="k"),
            ServerConfig(key="dst", type="jellyfin", url="http://dst", api_key="k"),
        ],
        users=[UserConfig(name="alex", accounts={"src": "u1", "dst": "u1"})],
        sync=SyncConfig(conflict_strategy=strategy, pairs=pairs),
    )


def test_one_way_pushes_source_state_to_target(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    guids = GuidSet(imdb="tt1")
    src.add_item("s1", MOVIE, guids)
    dst.add_item("d1", MOVIE, guids)
    src.set_state("u1", "s1", played=True)

    config = make_config([SyncPairConfig(name="p", source="src", target="dst", direction=Direction.ONE_WAY)])
    stats = SyncEngine(config, db, {"src": src, "dst": dst}).run()

    assert stats.pushed == 1
    assert dst.watch_state[("u1", "d1")]["played"] is True


def test_one_way_never_pushes_from_target_back_to_source(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    guids = GuidSet(imdb="tt1")
    src.add_item("s1", MOVIE, guids)
    dst.add_item("d1", MOVIE, guids)
    dst.set_state("u1", "d1", played=True)  # only the target has watched it

    config = make_config([SyncPairConfig(name="p", source="src", target="dst", direction=Direction.ONE_WAY)])
    stats = SyncEngine(config, db, {"src": src, "dst": dst}).run()

    assert stats.pushed == 0
    assert dst.push_calls == []


def test_one_way_skips_when_target_library_lacks_item(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    src.add_item("s1", MOVIE, GuidSet(imdb="tt1"))
    # dst has no matching item at all
    src.set_state("u1", "s1", played=True)

    config = make_config([SyncPairConfig(name="p", source="src", target="dst", direction=Direction.ONE_WAY)])
    stats = SyncEngine(config, db, {"src": src, "dst": dst}).run()

    assert stats.pushed == 0
    assert stats.skipped_no_match == 1


def test_one_way_dry_run_does_not_call_set_watch_state(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    guids = GuidSet(imdb="tt1")
    src.add_item("s1", MOVIE, guids)
    dst.add_item("d1", MOVIE, guids)
    src.set_state("u1", "s1", played=True)

    config = make_config([SyncPairConfig(name="p", source="src", target="dst", direction=Direction.ONE_WAY)])
    stats = SyncEngine(config, db, {"src": src, "dst": dst}).run(dry_run=True)

    assert stats.dry_run_pushes == 1
    assert dst.push_calls == []


def test_bidirectional_most_watched_prefers_further_progress(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    guids = GuidSet(imdb="tt1")
    src.add_item("s1", MOVIE, guids)
    dst.add_item("d1", MOVIE, guids)
    src.set_state("u1", "s1", played=False, view_offset_ms=1_000_000)
    dst.set_state("u1", "d1", played=True, view_offset_ms=0)  # fully played beats partial progress

    config = make_config(
        [SyncPairConfig(name="p", source="src", target="dst", direction=Direction.BIDIRECTIONAL)]
    )
    stats = SyncEngine(config, db, {"src": src, "dst": dst}).run()

    assert stats.pushed == 1
    assert src.watch_state[("u1", "s1")]["played"] is True  # target's state won and was pushed onto source


def test_bidirectional_latest_strategy_uses_last_played_at(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    guids = GuidSet(imdb="tt1")
    src.add_item("s1", MOVIE, guids)
    dst.add_item("d1", MOVIE, guids)
    src.set_state("u1", "s1", played=True, last_played_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    dst.set_state("u1", "d1", played=False, view_offset_ms=500_000, last_played_at=datetime(2026, 3, 1, tzinfo=timezone.utc))

    config = make_config(
        [
            SyncPairConfig(
                name="p", source="src", target="dst", direction=Direction.BIDIRECTIONAL,
                conflict_strategy=ConflictStrategy.LATEST,
            )
        ]
    )
    stats = SyncEngine(config, db, {"src": src, "dst": dst}).run()

    assert stats.pushed == 1
    # dst was more recently played, so despite lower progress it wins under `latest`
    assert src.watch_state[("u1", "s1")]["played"] is False
    assert src.watch_state[("u1", "s1")]["view_offset_ms"] == 500_000


def test_bidirectional_fills_in_missing_side_without_conflict_logic(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    guids = GuidSet(imdb="tt1")
    src.add_item("s1", MOVIE, guids)
    dst.add_item("d1", MOVIE, guids)
    src.set_state("u1", "s1", played=True)  # dst has never seen this item

    config = make_config(
        [SyncPairConfig(name="p", source="src", target="dst", direction=Direction.BIDIRECTIONAL)]
    )
    stats = SyncEngine(config, db, {"src": src, "dst": dst}).run()

    assert stats.pushed == 1
    assert dst.watch_state[("u1", "d1")]["played"] is True


def test_unchanged_state_is_not_repushed(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    guids = GuidSet(imdb="tt1")
    src.add_item("s1", MOVIE, guids)
    dst.add_item("d1", MOVIE, guids)
    src.set_state("u1", "s1", played=True)
    dst.set_state("u1", "d1", played=True)

    config = make_config([SyncPairConfig(name="p", source="src", target="dst", direction=Direction.ONE_WAY)])
    stats = SyncEngine(config, db, {"src": src, "dst": dst}).run()

    assert stats.pushed == 0
    assert stats.unchanged == 1


def test_second_run_is_idempotent(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    guids = GuidSet(imdb="tt1")
    src.add_item("s1", MOVIE, guids)
    dst.add_item("d1", MOVIE, guids)
    src.set_state("u1", "s1", played=True)

    config = make_config([SyncPairConfig(name="p", source="src", target="dst", direction=Direction.ONE_WAY)])
    engine = SyncEngine(config, db, {"src": src, "dst": dst})

    first = engine.run()
    second = engine.run()

    assert first.pushed == 1
    assert second.pushed == 0
    assert second.unchanged == 1


def test_episode_matching_across_servers(db):
    src, dst = FakeClient("src"), FakeClient("dst")
    show_guids = GuidSet(tvdb="81189")
    ep_info = EpisodeInfo(show_guids=show_guids, season_number=1, episode_number=1, show_title="Breaking Bad")
    src.add_item("s-e1", EPISODE, GuidSet(), episode=ep_info)
    dst.add_item("d-e1", EPISODE, GuidSet(), episode=ep_info)
    src.set_state("u1", "s-e1", played=True)

    config = make_config([SyncPairConfig(name="p", source="src", target="dst", direction=Direction.ONE_WAY)])
    stats = SyncEngine(config, db, {"src": src, "dst": dst}).run()

    assert stats.pushed == 1
    assert dst.watch_state[("u1", "d-e1")]["played"] is True

    # Log message shows "Show S01E01", not the (fake) episode-level title,
    # alongside which (config-level) person it was synced for, and the
    # resolved target item id -- so a wrong-item match (e.g. a duplicate
    # library entry) is visible directly in the log.
    push_lines = [line for line in stats.log if line.startswith("Pushed")]
    assert push_lines == [
        "Pushed 'Breaking Bad S01E01' (alex): src -> dst (played=True offset_ms=0 target_item_id=d-e1)"
    ]


def test_push_failure_on_cached_target_clears_it_for_next_resolve(db):
    """A 401/403/404 on a *cached* target id (one already pushed to
    successfully before, not freshly resolved this run) means that id is
    probably stale now -- e.g. the item was deleted and recreated on the
    target server -- not that the server is transiently down. Without
    clearing the cache, every future cycle would retry the same dead id and
    fail identically forever; it should instead drop the mapping so the
    next run re-resolves via guids and finds the item's new id.
    """
    src, dst = FakeClient("src"), FlakyClient("dst")
    guids = GuidSet(imdb="tt1")
    src.add_item("s1", MOVIE, guids)
    dst.add_item("d1", MOVIE, guids)
    src.set_state("u1", "s1", played=True)

    config = make_config([SyncPairConfig(name="p", source="src", target="dst", direction=Direction.ONE_WAY)])
    engine = SyncEngine(config, db, {"src": src, "dst": dst})

    first = engine.run()
    assert first.pushed == 1
    assert dst.watch_state[("u1", "d1")]["played"] is True

    # The item gets deleted and recreated under a new id on the target
    # server -- the old cached mapping (to d1) is now permanently invalid.
    del dst.library["d1"]
    del dst.watch_state[("u1", "d1")]
    dst.add_item("d2", MOVIE, guids)
    dst.fail_item_ids.add("d1")
    # played flips so the cached target state ("played=True") now visibly
    # differs and a re-push is attempted (an unplayed/offset-only change
    # within VIEW_OFFSET_TOLERANCE_MS wouldn't trigger one).
    src.set_state("u1", "s1", played=False, view_offset_ms=600_000)

    second = engine.run()
    assert second.pushed == 0
    assert second.errors == 1

    third = engine.run()
    assert third.pushed == 1
    assert dst.watch_state[("u1", "d2")]["played"] is False
