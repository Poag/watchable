"""Plex Media Server client.

Notes on how this maps onto Plex's actual HTTP API (there's no official
"watchable" endpoint, so this is built from PMS's REST interface as used by
tools like Tautulli and Kometa):

- Every request needs ``X-Plex-Token``. Crucially, watch state (``viewCount``,
  ``viewOffset``) is scoped to *whichever token makes the request* -- Plex
  has no "get user X's watch state" call for an admin token to use on
  someone else's behalf. So for Plex specifically, each synced person's
  ``accounts.<server>`` value in the config must be *their own* Plex token,
  not a username. See ``docs/PROVIDERS.md``.
- Items are matched across servers via the ``Guid`` array PMS attaches to
  metadata (``imdb://...``, ``tmdb://...``, ``tvdb://...``), requested here
  with ``includeGuids=1``.
- Marking watched/unwatched/in-progress uses the classic ``/:/scrobble``,
  ``/:/unscrobble``, and ``/:/progress`` endpoints.
- Cross-server lookup uses ``/library/all?guid=<scheme>://<value>``, a
  PMS-wide guid search added in newer server versions.
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

_TYPE_FOR_MEDIA = {MOVIE: 1, EPISODE: 4}
_TYPE_SHOW = 2


class PlexClient(MediaServerClient):
    server_type = "plex"

    def __init__(
        self, *, name: str, base_url: str, token: str, verify_tls: bool = True, timeout: float = 15.0
    ):
        super().__init__(name=name, base_url=base_url, verify_tls=verify_tls, timeout=timeout)
        self.token = token

    # -- HTTP plumbing ---------------------------------------------------

    def _request(
        self, method: str, path: str, *, user_token: str | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"X-Plex-Token": user_token or self.token, "Accept": "application/json"}
        try:
            resp = requests.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                params=params,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"Plex {self.name}: {method} {path} failed: {exc}") from exc
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"Plex {self.name}: non-JSON response from {path}") from exc

    def _get(self, path: str, *, user_token: str | None = None, params: dict[str, Any] | None = None):
        return self._request("GET", path, user_token=user_token, params=params)

    # -- MediaServerClient interface -------------------------------------

    def test_connection(self) -> None:
        self._get("/identity")

    def list_users(self) -> Iterable[tuple[str, str]]:
        data = self._get("/accounts")
        for account in data.get("MediaContainer", {}).get("Account", []):
            yield str(account["id"]), account.get("name") or f"user{account['id']}"

    def iter_watch_state(
        self, server_user_id: str, media_types: Iterable[MediaType] = SUPPORTED_MEDIA_TYPES
    ) -> Iterator[WatchStateRecord]:
        media_types = tuple(media_types)
        sections = self._get("/library/sections", user_token=server_user_id)
        for directory in sections.get("MediaContainer", {}).get("Directory", []):
            section_type = directory.get("type")
            section_key = directory["key"]
            if section_type == "movie" and MOVIE in media_types:
                yield from self._iter_section_movies(section_key, server_user_id)
            elif section_type == "show" and EPISODE in media_types:
                yield from self._iter_section_episodes(section_key, server_user_id)

    def _iter_section_movies(self, section_key: str, token: str) -> Iterator[WatchStateRecord]:
        data = self._get(
            f"/library/sections/{section_key}/all",
            user_token=token,
            params={"type": _TYPE_FOR_MEDIA[MOVIE], "includeGuids": 1},
        )
        for video in data.get("MediaContainer", {}).get("Metadata", []):
            if not _is_touched(video):
                continue
            yield self._record_from_video(video, MOVIE, episode=None, guids=_extract_guids(video))

    def _iter_section_episodes(self, section_key: str, token: str) -> Iterator[WatchStateRecord]:
        data = self._get(
            f"/library/sections/{section_key}/all",
            user_token=token,
            params={"type": _TYPE_FOR_MEDIA[EPISODE], "includeGuids": 1},
        )
        show_guid_cache: dict[str, GuidSet] = {}
        for video in data.get("MediaContainer", {}).get("Metadata", []):
            if not _is_touched(video):
                continue
            grandparent_key = str(video.get("grandparentRatingKey", ""))
            if grandparent_key and grandparent_key not in show_guid_cache:
                show_guid_cache[grandparent_key] = self._fetch_guids_for(grandparent_key, token)
            show_guids = show_guid_cache.get(grandparent_key, GuidSet())
            episode = EpisodeInfo(
                show_guids=show_guids,
                season_number=int(video.get("parentIndex", 0) or 0),
                episode_number=int(video.get("index", 0) or 0),
            )
            yield self._record_from_video(video, EPISODE, episode=episode, guids=GuidSet())

    def _fetch_guids_for(self, rating_key: str, token: str) -> GuidSet:
        data = self._get(f"/library/metadata/{rating_key}", user_token=token, params={"includeGuids": 1})
        metas = data.get("MediaContainer", {}).get("Metadata", [])
        return _extract_guids(metas[0]) if metas else GuidSet()

    @staticmethod
    def _record_from_video(
        video: dict[str, Any], media_type: MediaType, *, episode: EpisodeInfo | None, guids: GuidSet
    ) -> WatchStateRecord:
        view_count = int(video.get("viewCount", 0) or 0)
        view_offset = int(video.get("viewOffset", 0) or 0)
        duration = video.get("duration")
        last_viewed_epoch = video.get("lastViewedAt")
        return WatchStateRecord(
            server_item_id=str(video["ratingKey"]),
            media_type=media_type,
            title=video.get("title", "Unknown"),
            item_guids=guids,
            episode=episode,
            server_user_id="",
            played=view_count > 0,
            view_offset_ms=view_offset,
            runtime_ms=int(duration) if duration else None,
            last_played_at=(
                datetime.fromtimestamp(int(last_viewed_epoch), tz=timezone.utc)
                if last_viewed_epoch
                else None
            ),
        )

    def set_watch_state(
        self,
        server_user_id: str,
        server_item_id: str,
        *,
        played: bool,
        view_offset_ms: int,
        runtime_ms: int | None,
    ) -> None:
        common = {"key": server_item_id, "identifier": "com.plexapp.plugins.library"}
        if played:
            self._get("/:/scrobble", user_token=server_user_id, params=common)
        elif view_offset_ms > 0:
            self._get(
                "/:/progress",
                user_token=server_user_id,
                params={**common, "time": view_offset_ms, "state": "stopped"},
            )
        else:
            self._get("/:/unscrobble", user_token=server_user_id, params=common)

    def find_item_by_guids(
        self, media_type: MediaType, item_guids: GuidSet, episode: EpisodeInfo | None = None
    ) -> str | None:
        if media_type == EPISODE:
            if episode is None:
                return None
            show_key = self._find_by_guid(_TYPE_SHOW, episode.show_guids)
            if show_key is None:
                return None
            return self._find_episode_under_show(show_key, episode.season_number, episode.episode_number)
        return self._find_by_guid(_TYPE_FOR_MEDIA[media_type], item_guids)

    def _find_by_guid(self, plex_type: int, guids: GuidSet) -> str | None:
        for scheme, value in guids.items():
            try:
                data = self._get(
                    "/library/all", params={"type": plex_type, "guid": f"{scheme}://{value}"}
                )
            except ProviderError:
                continue
            metas = data.get("MediaContainer", {}).get("Metadata", [])
            if metas:
                return str(metas[0]["ratingKey"])
        return None

    def _find_episode_under_show(
        self, show_rating_key: str, season_number: int, episode_number: int
    ) -> str | None:
        data = self._get(f"/library/metadata/{show_rating_key}/allLeaves")
        for video in data.get("MediaContainer", {}).get("Metadata", []):
            if video.get("parentIndex") == season_number and video.get("index") == episode_number:
                return str(video["ratingKey"])
        return None


def _is_touched(video: dict[str, Any]) -> bool:
    return bool(video.get("viewCount") or video.get("viewOffset"))


def _extract_guids(video: dict[str, Any]) -> GuidSet:
    imdb = tmdb = tvdb = None
    for guid_obj in video.get("Guid", []) or []:
        gid = guid_obj.get("id", "")
        if gid.startswith("imdb://"):
            imdb = gid[len("imdb://") :]
        elif gid.startswith("tmdb://"):
            tmdb = gid[len("tmdb://") :]
        elif gid.startswith("tvdb://"):
            tvdb = gid[len("tvdb://") :]
    return GuidSet(imdb=imdb, tmdb=tmdb, tvdb=tvdb)
