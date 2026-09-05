"""Cross-server item identity.

Two media servers never agree on an item's internal ID, so the only way to
know that Plex's ``ratingKey=4821`` and Jellyfin's ``itemId=e3f1...`` are "the
same movie" is to compare externally-assigned identifiers (IMDb/TMDb/TVDb).
This module turns a provider's :class:`~watchable.providers.base.GuidSet` (and,
for episodes, season/episode numbers) into the canonical string keys the
database uses to recognize "same item", and back again.
"""

from __future__ import annotations

import re

from watchable.providers.base import EPISODE, EpisodeInfo, GuidSet, MediaType

_EPISODE_KEY_RE = re.compile(
    r"^(?P<scheme>imdb|tmdb|tvdb):(?P<value>[^:]+):S(?P<season>\d+)E(?P<episode>\d+)$"
)
_MOVIE_KEY_RE = re.compile(r"^(?P<scheme>imdb|tmdb|tvdb):(?P<value>[^:]+)$")


def build_guid_keys(
    media_type: MediaType, item_guids: GuidSet, episode: EpisodeInfo | None = None
) -> tuple[str, ...]:
    """Build the canonical matching keys for one item.

    A movie with both an imdb and tmdb id gets two keys pointing at the same
    database row (``resolve_or_create_item`` merges on any overlap), so a
    target server that only exposes one of the two schemes still matches.
    """
    if media_type == EPISODE:
        if episode is None:
            raise ValueError("episode media_type requires an EpisodeInfo")
        suffix = f"S{episode.season_number:02d}E{episode.episode_number:02d}"
        return tuple(f"{key}:{suffix}" for key in episode.show_guids.canonical_keys())
    return item_guids.canonical_keys()


def is_matchable(media_type: MediaType, item_guids: GuidSet, episode: EpisodeInfo | None = None) -> bool:
    """Whether we have enough external ID info to ever match this item cross-server."""
    if media_type == EPISODE:
        return episode is not None and not episode.show_guids.is_empty()
    return not item_guids.is_empty()


def parse_guid_key(key: str) -> tuple[str, str, int | None, int | None]:
    """Inverse of build_guid_keys for a single key: (scheme, value, season, episode)."""
    match = _EPISODE_KEY_RE.match(key)
    if match:
        return (
            match.group("scheme"),
            match.group("value"),
            int(match.group("season")),
            int(match.group("episode")),
        )
    match = _MOVIE_KEY_RE.match(key)
    if match:
        return (match.group("scheme"), match.group("value"), None, None)
    raise ValueError(f"Not a recognized guid key: {key!r}")


def guids_from_stored_keys(keys: list[str]) -> tuple[GuidSet, EpisodeInfo | None]:
    """Rebuild a GuidSet (+ EpisodeInfo, if these are episode keys) from stored keys.

    Used when we need to re-query a server for an item we already know about
    (e.g. resolving where it lives on a target server) without having a live
    WatchStateRecord in hand.
    """
    imdb = tmdb = tvdb = None
    season = episode_number = None
    for key in keys:
        scheme, value, s, e = parse_guid_key(key)
        if scheme == "imdb":
            imdb = value
        elif scheme == "tmdb":
            tmdb = value
        elif scheme == "tvdb":
            tvdb = value
        if s is not None:
            season, episode_number = s, e

    guids = GuidSet(imdb=imdb, tmdb=tmdb, tvdb=tvdb)
    if season is not None and episode_number is not None:
        return GuidSet(), EpisodeInfo(show_guids=guids, season_number=season, episode_number=episode_number)
    return guids, None
