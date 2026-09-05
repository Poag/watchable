from __future__ import annotations

import responses

from watchable.providers.base import MOVIE, GuidSet
from watchable.providers.plex import PlexClient

BASE = "http://plex.example.lan:32400"


def make_client() -> PlexClient:
    return PlexClient(name="plex", base_url=BASE, token="admintoken")


@responses.activate
def test_iter_watch_state_movies_uses_user_token_header():
    responses.add(
        responses.GET,
        f"{BASE}/library/sections",
        json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}},
    )
    responses.add(
        responses.GET,
        f"{BASE}/library/sections/1/all",
        json={
            "MediaContainer": {
                "Metadata": [
                    {
                        "ratingKey": "100",
                        "title": "Some Movie",
                        "viewCount": 1,
                        "viewOffset": 0,
                        "duration": 7_200_000,
                        "lastViewedAt": 1_700_000_000,
                        "Guid": [{"id": "imdb://tt0111161"}, {"id": "tmdb://278"}],
                    },
                    {"ratingKey": "200", "title": "Never watched", "Guid": [{"id": "imdb://tt9999999"}]},
                ]
            }
        },
    )
    client = make_client()
    records = list(client.iter_watch_state("personal-user-token", media_types=(MOVIE,)))

    assert len(records) == 1  # the untouched movie is skipped
    record = records[0]
    assert record.server_item_id == "100"
    assert record.played is True
    assert record.item_guids == GuidSet(imdb="tt0111161", tmdb="278")
    assert record.runtime_ms == 7_200_000

    # Every request should have used the per-user token, not the admin token.
    for call in responses.calls:
        assert call.request.headers["X-Plex-Token"] == "personal-user-token"


@responses.activate
def test_set_watch_state_played_calls_scrobble():
    responses.add(responses.GET, f"{BASE}/:/scrobble", body="")
    client = make_client()
    client.set_watch_state("usertoken", "100", played=True, view_offset_ms=0, runtime_ms=None)
    assert "/:/scrobble" in responses.calls[0].request.url
    assert "key=100" in responses.calls[0].request.url


@responses.activate
def test_set_watch_state_in_progress_calls_progress():
    responses.add(responses.GET, f"{BASE}/:/progress", body="")
    client = make_client()
    client.set_watch_state("usertoken", "100", played=False, view_offset_ms=60_000, runtime_ms=7_200_000)
    assert "/:/progress" in responses.calls[0].request.url
    assert "time=60000" in responses.calls[0].request.url


@responses.activate
def test_find_item_by_guids_movie():
    responses.add(
        responses.GET,
        f"{BASE}/library/all",
        json={"MediaContainer": {"Metadata": [{"ratingKey": "100"}]}},
    )
    client = make_client()
    result = client.find_item_by_guids(MOVIE, GuidSet(imdb="tt0111161"))
    assert result == "100"
    assert "guid=imdb%3A%2F%2Ftt0111161" in responses.calls[0].request.url
