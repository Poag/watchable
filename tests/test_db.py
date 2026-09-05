from __future__ import annotations

from datetime import datetime, timezone

from watchable.providers.base import MOVIE, GuidSet, WatchStateRecord


def _record(**overrides) -> WatchStateRecord:
    defaults = dict(
        server_item_id="123",
        media_type=MOVIE,
        title="The Shawshank Redemption",
        item_guids=GuidSet(imdb="tt0111161"),
        episode=None,
        server_user_id="u1",
        played=True,
        view_offset_ms=0,
        runtime_ms=8_520_000,
        last_played_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return WatchStateRecord(**defaults)


def test_resolve_or_create_item_creates_new(db):
    item_id = db.resolve_or_create_item(
        media_type="movie", title="Shawshank", year=1994, guid_keys=("imdb:tt0111161",)
    )
    assert item_id is not None
    same_id = db.resolve_or_create_item(
        media_type="movie", title="Shawshank", year=1994, guid_keys=("imdb:tt0111161",)
    )
    assert same_id == item_id


def test_resolve_or_create_item_matches_on_any_shared_key(db):
    first = db.resolve_or_create_item(
        media_type="movie", title="Shawshank", year=1994, guid_keys=("imdb:tt0111161", "tmdb:278")
    )
    # A server that only knows the tmdb id should still resolve to the same item.
    second = db.resolve_or_create_item(media_type="movie", title="Shawshank", year=1994, guid_keys=("tmdb:278",))
    assert second == first


def test_resolve_or_create_item_merges_when_overlap_discovered_later(db):
    a = db.resolve_or_create_item(media_type="movie", title="Shawshank", year=1994, guid_keys=("imdb:tt0111161",))
    b = db.resolve_or_create_item(media_type="movie", title="Shawshank", year=1994, guid_keys=("tmdb:278",))
    assert a != b  # no shared key yet -> two separate items

    db.record_watch_state(item_id=a, user_name="alex", server_key="plex", record=_record())
    db.record_watch_state(item_id=b, user_name="alex", server_key="jellyfin", record=_record(server_item_id="456"))

    # A third server reports both ids on the same item -> triggers a merge.
    merged = db.resolve_or_create_item(
        media_type="movie", title="Shawshank", year=1994, guid_keys=("imdb:tt0111161", "tmdb:278")
    )
    assert merged == min(a, b)

    # Watch state recorded against both original ids is now visible under the merged id.
    plex_state = db.get_watch_state(item_id=merged, user_name="alex", server_key="plex")
    jf_state = db.get_watch_state(item_id=merged, user_name="alex", server_key="jellyfin")
    assert plex_state is not None
    assert jf_state is not None


def test_record_and_get_watch_state_roundtrip(db):
    item_id = db.resolve_or_create_item(media_type="movie", title="Shawshank", year=1994, guid_keys=("imdb:tt1",))
    db.record_watch_state(
        item_id=item_id, user_name="alex", server_key="plex", record=_record(played=False, view_offset_ms=60_000)
    )
    state = db.get_watch_state(item_id=item_id, user_name="alex", server_key="plex")
    assert state is not None
    assert state.played is False
    assert state.view_offset_ms == 60_000
    assert state.server_item_id == "123"


def test_get_watch_state_missing_returns_none(db):
    assert db.get_watch_state(item_id=999, user_name="alex", server_key="plex") is None


def test_copy_watch_state(db):
    item_id = db.resolve_or_create_item(media_type="movie", title="Shawshank", year=1994, guid_keys=("imdb:tt1",))
    db.record_watch_state(item_id=item_id, user_name="alex", server_key="plex", record=_record())
    db.copy_watch_state(
        item_id=item_id, user_name="alex", from_server_key="plex", to_server_key="jellyfin", to_server_item_id="jf-1"
    )
    copied = db.get_watch_state(item_id=item_id, user_name="alex", server_key="jellyfin")
    assert copied is not None
    assert copied.server_item_id == "jf-1"
    assert copied.played is True


def test_sync_log_roundtrip(db):
    item_id = db.resolve_or_create_item(media_type="movie", title="Shawshank", year=1994, guid_keys=("imdb:tt1",))
    db.log_sync_action(
        pair_name="p2j", item_id=item_id, title="Shawshank", user_name="alex",
        source_server="plex", target_server="jellyfin", action="push", detail="played=True",
    )
    rows = db.recent_log(10)
    assert len(rows) == 1
    assert rows[0]["action"] == "push"
    assert rows[0]["pair_name"] == "p2j"
