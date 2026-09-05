"""Emby Server client.

Emby is the project Jellyfin forked from, and years later the two APIs are
still close enough that :class:`EmbyClient` is a thin subclass of
:class:`~watchable.providers.jellyfin.JellyfinClient`: same auth header,
same ``/Users``, ``/Items``, and ``PlayedItems`` shapes.

The one deliberate difference: Jellyfin's ``AnyProviderIdEquals`` query
parameter for exact provider-id lookup is a Jellyfin-only addition, so
cross-server matching here falls back to fetching each candidate item type
with ``Fields=ProviderIds`` and filtering client-side. That's heavier than a
server-side filter, but Emby libraries in a home-lab sync setup are small
enough that it's not a practical concern.
"""

from __future__ import annotations

from watchable.providers.base import EPISODE, EpisodeInfo, GuidSet, MediaType
from watchable.providers.jellyfin import JellyfinClient, _extract_guids


class EmbyClient(JellyfinClient):
    server_type = "emby"

    def find_item_by_guids(
        self, media_type: MediaType, item_guids: GuidSet, episode: EpisodeInfo | None = None
    ) -> str | None:
        if media_type == EPISODE:
            if episode is None:
                return None
            series_id = self._scan_for_guids("Series", episode.show_guids)
            if series_id is None:
                return None
            return self._find_episode_under_series(series_id, episode.season_number, episode.episode_number)
        return self._scan_for_guids("Movie", item_guids)

    def _scan_for_guids(self, item_type: str, guids: GuidSet) -> str | None:
        wanted = set(guids.items())
        if not wanted:
            return None
        data = self._request(
            "GET",
            "/Items",
            params={"Recursive": "true", "IncludeItemTypes": item_type, "Fields": "ProviderIds"},
        )
        for item in data.get("Items", []):
            if wanted & set(_extract_guids(item).items()):
                return item["Id"]
        return None
