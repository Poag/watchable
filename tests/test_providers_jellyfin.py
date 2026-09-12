from __future__ import annotations

import pytest
import responses

from watchable.providers.base import EPISODE, MOVIE, GuidSet, ProviderError
from watchable.providers.jellyfin import JellyfinClient

BASE = "http://jf.example.lan:8096"


def make_client() -> JellyfinClient:
    return JellyfinClient(name="jf", base_url=BASE, api_key="testkey")


@responses.activate
def test_list_users():
    responses.add(
        responses.GET,
        f"{BASE}/Users",
        json=[{"Id": "u1", "Name": "alex"}, {"Id": "u2", "Name": "sam"}],
    )
    client = make_client()
    users = list(client.list_users())
    assert users == [("u1", "alex"), ("u2", "sam")]
    assert responses.calls[0].request.headers["X-Emby-Token"] == "testkey"


@responses.activate
def test_iter_watch_state_skips_untouched_items():
    responses.add(
        responses.GET,
        f"{BASE}/Users/u1/Items",
        json={
            "Items": [
                {
                    "Id": "m1",
                    "Type": "Movie",
                    "Name": "Watched Movie",
                    "ProviderIds": {"Imdb": "tt0111161", "Tmdb": "278"},
                    "RunTimeTicks": 72_000_000_000,  # 7200s
                    "UserData": {"Played": True, "PlaybackPositionTicks": 0, "LastPlayedDate": "2026-02-01T10:00:00.0000000Z"},
                },
                {
                    "Id": "m2",
                    "Type": "Movie",
                    "Name": "Untouched Movie",
                    "ProviderIds": {"Imdb": "tt9999999"},
                    "UserData": {"Played": False, "PlaybackPositionTicks": 0},
                },
            ]
        },
    )
    client = make_client()
    records = list(client.iter_watch_state("u1", media_types=(MOVIE,)))

    assert len(records) == 1
    record = records[0]
    assert record.server_item_id == "m1"
    assert record.played is True
    assert record.item_guids == GuidSet(imdb="tt0111161", tmdb="278")
    assert record.runtime_ms == 7_200_000
    assert record.last_played_at is not None


@responses.activate
def test_iter_watch_state_episode_fetches_series_guids():
    responses.add(
        responses.GET,
        f"{BASE}/Users/u1/Items",
        json={
            "Items": [
                {
                    "Id": "e1",
                    "Type": "Episode",
                    "Name": "Pilot",
                    "SeriesId": "series1",
                    "ParentIndexNumber": 1,
                    "IndexNumber": 1,
                    "UserData": {"Played": True, "PlaybackPositionTicks": 0},
                }
            ]
        },
    )
    responses.add(
        responses.GET,
        f"{BASE}/Items/series1",
        json={"ProviderIds": {"Tvdb": "81189"}},
    )
    client = make_client()
    records = list(client.iter_watch_state("u1", media_types=(EPISODE,)))

    assert len(records) == 1
    record = records[0]
    assert record.episode is not None
    assert record.episode.show_guids == GuidSet(tvdb="81189")
    assert record.episode.season_number == 1
    assert record.episode.episode_number == 1
    # userId is required by some Jellyfin/Emby versions for the unscoped
    # /Items/{id} lookup -- always send it.
    assert "userId=u1" in responses.calls[-1].request.url


@responses.activate
def test_iter_watch_state_series_guid_failure_does_not_lose_other_items():
    responses.add(
        responses.GET,
        f"{BASE}/Users/u1/Items",
        json={
            "Items": [
                {
                    "Id": "e1",
                    "Type": "Episode",
                    "Name": "Pilot",
                    "SeriesId": "broken-series",
                    "ParentIndexNumber": 1,
                    "IndexNumber": 1,
                    "UserData": {"Played": True, "PlaybackPositionTicks": 0},
                },
                {
                    "Id": "m1",
                    "Type": "Movie",
                    "Name": "Some Movie",
                    "ProviderIds": {"Imdb": "tt0111161"},
                    "UserData": {"Played": True, "PlaybackPositionTicks": 0},
                },
            ]
        },
    )
    responses.add(responses.GET, f"{BASE}/Items/broken-series", status=400)
    client = make_client()

    records = list(client.iter_watch_state("u1", media_types=(EPISODE, MOVIE)))

    # The episode is still yielded (with no show guids, so it won't match
    # cross-server -- that's handled upstream) instead of the whole pull
    # for this user aborting on the one bad series lookup.
    assert {r.server_item_id for r in records} == {"e1", "m1"}
    episode_record = next(r for r in records if r.server_item_id == "e1")
    assert episode_record.episode is not None
    assert episode_record.episode.show_guids == GuidSet()


@responses.activate
def test_set_watch_state_played_calls_played_items_post():
    responses.add(responses.POST, f"{BASE}/Users/u1/PlayedItems/m1", json={})
    responses.add(
        responses.GET,
        f"{BASE}/Users/u1/Items",
        json={"Items": [{"Id": "m1", "UserData": {"Played": True}}]},
    )
    client = make_client()
    client.set_watch_state("u1", "m1", played=True, view_offset_ms=0, runtime_ms=None)
    assert responses.calls[0].request.method == "POST"
    assert responses.calls[0].request.url.endswith("/Users/u1/PlayedItems/m1")


@responses.activate
def test_set_watch_state_in_progress_calls_delete_then_userdata_post():
    responses.add(responses.DELETE, f"{BASE}/Users/u1/PlayedItems/m1", json={})
    responses.add(responses.POST, f"{BASE}/Users/u1/Items/m1/UserData", json={})
    responses.add(
        responses.GET,
        f"{BASE}/Users/u1/Items",
        json={"Items": [{"Id": "m1", "UserData": {"Played": False}}]},
    )
    client = make_client()
    client.set_watch_state("u1", "m1", played=False, view_offset_ms=60_000, runtime_ms=7_200_000)

    assert responses.calls[0].request.method == "DELETE"
    assert responses.calls[1].request.method == "POST"
    assert responses.calls[1].request.url.endswith("/Users/u1/Items/m1/UserData")


@responses.activate
def test_set_watch_state_raises_when_push_had_no_effect():
    # PlayedItems returns 2xx even for a user id that doesn't exist on this
    # server -- reading the item back and finding it unchanged is what
    # should actually surface the failure.
    responses.add(responses.POST, f"{BASE}/Users/bad-user/PlayedItems/m1", json={})
    responses.add(
        responses.GET,
        f"{BASE}/Users/bad-user/Items",
        json={"Items": [{"Id": "m1", "UserData": {"Played": False}}]},
    )
    client = make_client()

    with pytest.raises(ProviderError, match="still reports played=False"):
        client.set_watch_state("bad-user", "m1", played=True, view_offset_ms=0, runtime_ms=None)


@responses.activate
def test_set_watch_state_raises_when_item_missing_from_readback():
    # A userId that isn't a real account can also make the readback
    # itself come back empty rather than merely showing Played=false.
    responses.add(responses.POST, f"{BASE}/Users/bad-user/PlayedItems/m1", json={})
    responses.add(responses.GET, f"{BASE}/Users/bad-user/Items", json={"Items": []})
    client = make_client()

    with pytest.raises(ProviderError, match="still reports played=False"):
        client.set_watch_state("bad-user", "m1", played=True, view_offset_ms=0, runtime_ms=None)


@responses.activate
def test_find_item_by_guids_movie():
    responses.add(
        responses.GET,
        f"{BASE}/Items",
        json={"Items": [{"Id": "m1"}]},
        match=[responses.matchers.query_param_matcher(
            {"Recursive": "true", "IncludeItemTypes": "Movie", "AnyProviderIdEquals": "imdb.tt0111161"}
        )],
    )
    client = make_client()
    result = client.find_item_by_guids(MOVIE, GuidSet(imdb="tt0111161"))
    assert result == "m1"


@responses.activate
def test_find_item_by_guids_returns_none_when_not_found():
    responses.add(responses.GET, f"{BASE}/Items", json={"Items": []})
    client = make_client()
    result = client.find_item_by_guids(MOVIE, GuidSet(imdb="tt0000000"))
    assert result is None
