from __future__ import annotations

import responses

from watchable.providers.base import MOVIE, GuidSet
from watchable.providers.emby import EmbyClient

BASE = "http://emby.example.lan:8096"


def make_client() -> EmbyClient:
    return EmbyClient(name="emby", base_url=BASE, api_key="testkey")


def test_emby_shares_jellyfin_request_plumbing():
    # EmbyClient is a thin subclass -- confirm it didn't diverge on auth/type.
    client = make_client()
    assert client.server_type == "emby"
    assert client.auth_header == "X-Emby-Token"


@responses.activate
def test_find_item_by_guids_scans_client_side():
    # Emby doesn't get AnyProviderIdEquals -- EmbyClient falls back to fetching
    # candidates and filtering locally.
    responses.add(
        responses.GET,
        f"{BASE}/Items",
        json={
            "Items": [
                {"Id": "m1", "ProviderIds": {"Imdb": "tt0111161"}},
                {"Id": "m2", "ProviderIds": {"Imdb": "tt9999999"}},
            ]
        },
    )
    client = make_client()
    result = client.find_item_by_guids(MOVIE, GuidSet(imdb="tt0111161"))
    assert result == "m1"

    request_url = responses.calls[0].request.url
    assert "AnyProviderIdEquals" not in request_url


@responses.activate
def test_find_item_by_guids_returns_none_when_no_match():
    responses.add(responses.GET, f"{BASE}/Items", json={"Items": [{"Id": "m1", "ProviderIds": {"Imdb": "tt1"}}]})
    client = make_client()
    assert client.find_item_by_guids(MOVIE, GuidSet(imdb="tt2")) is None
