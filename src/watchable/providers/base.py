"""Common interface that every media server provider client implements.

Sync logic in :mod:`watchable.sync` only ever talks to this interface, so
adding a new media server means writing one new class here and registering
it in :mod:`watchable.providers` -- nothing else in the codebase needs to
change.
"""

from __future__ import annotations

import abc
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

import urllib3

logger = logging.getLogger("watchable.providers")

#: Media types watchable knows how to sync. Providers translate their own
#: type names (e.g. Plex's "movie"/"episode", Jellyfin's "Movie"/"Episode")
#: to and from these.
MediaType = str
MOVIE = "movie"
EPISODE = "episode"

SUPPORTED_MEDIA_TYPES = (MOVIE, EPISODE)


@dataclass(frozen=True)
class GuidSet:
    """External identifiers for a media item, as reported by a server.

    Every provider is expected to resolve at least one of these for an item
    whenever the underlying server exposes it, since these are what
    :mod:`watchable.matching` uses to recognize "the same movie/episode" on
    two different servers that assign it unrelated internal IDs.
    """

    imdb: str | None = None
    tmdb: str | None = None
    tvdb: str | None = None

    def canonical_keys(self) -> tuple[str, ...]:
        """Stable ``"scheme:value"`` keys usable as a matching key / dict key."""
        keys = []
        if self.imdb:
            keys.append(f"imdb:{self.imdb}")
        if self.tmdb:
            keys.append(f"tmdb:{self.tmdb}")
        if self.tvdb:
            keys.append(f"tvdb:{self.tvdb}")
        return tuple(keys)

    def is_empty(self) -> bool:
        return not self.canonical_keys()

    def items(self) -> Iterator[tuple[str, str]]:
        """Yield (scheme, value) for every id present, in imdb/tmdb/tvdb priority order."""
        if self.imdb:
            yield ("imdb", self.imdb)
        if self.tmdb:
            yield ("tmdb", self.tmdb)
        if self.tvdb:
            yield ("tvdb", self.tvdb)


@dataclass(frozen=True)
class EpisodeInfo:
    """Extra identifying info for episodes, used alongside a show-level GuidSet."""

    show_guids: GuidSet
    season_number: int
    episode_number: int


@dataclass(frozen=True)
class WatchStateRecord:
    """One (item, user) watch-state observation pulled from a server.

    ``item_guids`` identifies the movie itself, or (for episodes) is the
    show's GuidSet paired with ``episode`` season/episode numbers -- TV
    episodes rarely carry their own imdb/tmdb id, but "show X, S02E05" is
    just as good a matching key.
    """

    server_item_id: str
    media_type: MediaType
    title: str
    item_guids: GuidSet
    episode: EpisodeInfo | None
    server_user_id: str
    played: bool
    view_offset_ms: int
    runtime_ms: int | None
    last_played_at: datetime | None
    updated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def progress_fraction(self) -> float:
        if not self.runtime_ms:
            return 1.0 if self.played else 0.0
        return min(1.0, max(0.0, self.view_offset_ms / self.runtime_ms))


class ProviderError(RuntimeError):
    """Raised for any provider-side failure (auth, network, unexpected payload)."""


class MediaServerClient(abc.ABC):
    """Base class every Plex/Jellyfin/Emby-style client implements."""

    server_type: str = "base"

    def __init__(self, *, name: str, base_url: str, verify_tls: bool = True, timeout: float = 15.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.verify_tls = verify_tls
        self.timeout = timeout
        if not verify_tls:
            # requests/urllib3 otherwise emit InsecureRequestWarning on every
            # single unverified request -- verify_tls=False is a config
            # choice the operator already made explicitly (self-signed local
            # certs), so note it once here instead of spamming the log.
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            logger.warning("TLS certificate verification is disabled for server %r", name)

    @abc.abstractmethod
    def test_connection(self) -> None:
        """Raise ProviderError if the server can't be reached/authenticated."""

    @abc.abstractmethod
    def list_users(self) -> Iterable[tuple[str, str]]:
        """Yield (server_user_id, display_name) for every account on the server."""

    @abc.abstractmethod
    def iter_watch_state(
        self, server_user_id: str, media_types: Iterable[MediaType] = SUPPORTED_MEDIA_TYPES
    ) -> Iterator[WatchStateRecord]:
        """Yield a WatchStateRecord for every item this user has touched.

        "Touched" means played at least once, or has partial progress -- items
        never started are omitted, since there's no watch state to sync.
        """

    @abc.abstractmethod
    def set_watch_state(
        self,
        server_user_id: str,
        server_item_id: str,
        *,
        played: bool,
        view_offset_ms: int,
        runtime_ms: int | None,
    ) -> None:
        """Push a watch state onto the server for one item/user.

        Implementations should be idempotent: calling this with the state the
        server already has should be a cheap no-op or harmless overwrite.
        """

    @abc.abstractmethod
    def find_item_by_guids(
        self,
        media_type: MediaType,
        item_guids: GuidSet,
        episode: EpisodeInfo | None = None,
    ) -> str | None:
        """Resolve a matching item's server_item_id on *this* server, if present.

        Returns None if this server's library doesn't have the item at all
        (e.g. a movie the user watched on Plex but which was never added to
        the Jellyfin library) -- callers must treat that as "nothing to sync
        to here", not an error.
        """

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r} url={self.base_url!r}>"
