"""Jellyfin Media Server client.

Jellyfin exposes user-scoped watch state directly (unlike Plex, one API key
can read and write any user's data), which makes this the more
straightforward of the three providers:

- Auth: ``X-Emby-Token: <api key>`` header (kept from its Emby ancestry).
- Users: ``GET /Users``.
- Watch state: ``GET /Users/{userId}/Items`` with ``Fields=ProviderIds`` --
  each item DTO carries its own ``UserData.Played`` /
  ``UserData.PlaybackPositionTicks`` for the requesting user, no per-user
  token needed.
- Marking watched/unwatched: ``POST``/``DELETE /Users/{userId}/PlayedItems/{id}``.
- Setting progress: ``POST /Users/{userId}/Items/{id}/UserData``.
- Cross-server lookup: ``GET /Items?AnyProviderIdEquals=<scheme>.<value>``.
  This query parameter was added to Jellyfin's Items endpoint for exact
  provider-id matching; verify it against your server version (see
  ``docs/PROVIDERS.md``) -- ``EmbyClient`` overrides lookup with a
  client-side scan since Emby doesn't support it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

import requests

from watchable.providers.base import (
    EPISODE,
    MOVIE,
    SUPPORTED_MEDIA_TYPES,
    EpisodeInfo,
    GuidSet,
    MediaServerClient,
    MediaType,
    ProviderError,
    WatchStateRecord,
)

_ITEM_TYPE_FOR_MEDIA = {MOVIE: "Movie", EPISODE: "Episode"}
_TICKS_PER_MS = 10_000  # Jellyfin/Emby ticks are 100ns units


def _ticks_to_ms(ticks: int | None) -> int:
    return int(ticks or 0) // _TICKS_PER_MS


def _ms_to_ticks(ms: int) -> int:
    return int(ms) * _TICKS_PER_MS


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Jellyfin/Emby emit e.g. "2026-03-01T12:34:56.0000000Z"; trim to microseconds.
        cleaned = value.rstrip("Z")
        if "." in cleaned:
            head, frac = cleaned.split(".", 1)
            cleaned = f"{head}.{frac[:6]}"
        return datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_guids(item: dict[str, Any]) -> GuidSet:
    provider_ids = item.get("ProviderIds") or {}
    lookup = {k.lower(): v for k, v in provider_ids.items()}
    return GuidSet(imdb=lookup.get("imdb"), tmdb=lookup.get("tmdb"), tvdb=lookup.get("tvdb"))


class JellyfinClient(MediaServerClient):
    server_type = "jellyfin"
    auth_header = "X-Emby-Token"

    def __init__(
        self, *, name: str, base_url: str, api_key: str, verify_tls: bool = True, timeout: float = 15.0
    ):
        super().__init__(name=name, base_url=base_url, verify_tls=verify_tls, timeout=timeout)
        self.api_key = api_key

    # -- HTTP plumbing -----------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        headers = {self.auth_header: self.api_key, "Accept": "application/json"}
        try:
            resp = requests.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"{self.server_type} {self.name}: {method} {path} failed: {exc}") from exc
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.server_type} {self.name}: non-JSON response from {path}") from exc

    # -- MediaServerClient interface ---------------------------------------

    def test_connection(self) -> None:
        self._request("GET", "/System/Info")

    def list_users(self) -> Iterable[tuple[str, str]]:
        for user in self._request("GET", "/Users"):
            yield user["Id"], user.get("Name", user["Id"])

    def iter_watch_state(
        self, server_user_id: str, media_types: Iterable[MediaType] = SUPPORTED_MEDIA_TYPES
    ) -> Iterator[WatchStateRecord]:
        item_types = [_ITEM_TYPE_FOR_MEDIA[m] for m in media_types if m in _ITEM_TYPE_FOR_MEDIA]
        if not item_types:
            return
        data = self._request(
            "GET",
            f"/Users/{server_user_id}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": ",".join(item_types),
                "Fields": "ProviderIds",
            },
        )
        series_guid_cache: dict[str, GuidSet] = {}
        for item in data.get("Items", []):
            record = self._record_from_item(item, server_user_id, series_guid_cache)
            if record is not None:
                yield record

    def _record_from_item(
        self, item: dict[str, Any], server_user_id: str, series_guid_cache: dict[str, GuidSet]
    ) -> WatchStateRecord | None:
        user_data = item.get("UserData") or {}
        played = bool(user_data.get("Played"))
        position_ticks = int(user_data.get("PlaybackPositionTicks", 0) or 0)
        if not played and position_ticks <= 0:
            return None  # never touched -- nothing to sync

        media_type = MOVIE if item.get("Type") == "Movie" else EPISODE
        item_guids = GuidSet()
        episode_info = None
        if media_type == EPISODE:
            series_id = item.get("SeriesId")
            if series_id:
                if series_id not in series_guid_cache:
                    series_guid_cache[series_id] = self._fetch_item_guids(series_id)
                show_guids = series_guid_cache[series_id]
            else:
                show_guids = GuidSet()
            episode_info = EpisodeInfo(
                show_guids=show_guids,
                season_number=int(item.get("ParentIndexNumber", 0) or 0),
                episode_number=int(item.get("IndexNumber", 0) or 0),
            )
        else:
            item_guids = _extract_guids(item)

        runtime_ticks = item.get("RunTimeTicks")
        return WatchStateRecord(
            server_item_id=item["Id"],
            media_type=media_type,
            title=item.get("Name", "Unknown"),
            item_guids=item_guids,
            episode=episode_info,
            server_user_id=server_user_id,
            played=played,
            view_offset_ms=_ticks_to_ms(position_ticks),
            runtime_ms=_ticks_to_ms(runtime_ticks) if runtime_ticks else None,
            last_played_at=_parse_iso(user_data.get("LastPlayedDate")),
        )

    def _fetch_item_guids(self, item_id: str) -> GuidSet:
        data = self._request("GET", f"/Items/{item_id}", params={"Fields": "ProviderIds"})
        return _extract_guids(data)

    def set_watch_state(
        self,
        server_user_id: str,
        server_item_id: str,
        *,
        played: bool,
        view_offset_ms: int,
        runtime_ms: int | None,
    ) -> None:
        method = "POST" if played else "DELETE"
        self._request(method, f"/Users/{server_user_id}/PlayedItems/{server_item_id}")
        if not played and view_offset_ms > 0:
            self._request(
                "POST",
                f"/Users/{server_user_id}/Items/{server_item_id}/UserData",
                json_body={"PlaybackPositionTicks": _ms_to_ticks(view_offset_ms)},
            )

    def find_item_by_guids(
        self, media_type: MediaType, item_guids: GuidSet, episode: EpisodeInfo | None = None
    ) -> str | None:
        if media_type == EPISODE:
            if episode is None:
                return None
            series_id = self._find_id_by_guids("Series", episode.show_guids)
            if series_id is None:
                return None
            return self._find_episode_under_series(series_id, episode.season_number, episode.episode_number)
        return self._find_id_by_guids("Movie", item_guids)

    def _find_id_by_guids(self, item_type: str, guids: GuidSet) -> str | None:
        for scheme, value in guids.items():
            try:
                data = self._request(
                    "GET",
                    "/Items",
                    params={
                        "Recursive": "true",
                        "IncludeItemTypes": item_type,
                        "AnyProviderIdEquals": f"{scheme}.{value}",
                    },
                )
            except ProviderError:
                continue
            items = data.get("Items", [])
            if items:
                return items[0]["Id"]
        return None

    def _find_episode_under_series(
        self, series_id: str, season_number: int, episode_number: int
    ) -> str | None:
        data = self._request("GET", f"/Shows/{series_id}/Episodes", params={"Fields": "ProviderIds"})
        for item in data.get("Items", []):
            if item.get("ParentIndexNumber") == season_number and item.get("IndexNumber") == episode_number:
                return item["Id"]
        return None
