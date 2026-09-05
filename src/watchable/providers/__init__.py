"""Media server provider clients."""

from watchable.providers.base import MediaServerClient, WatchStateRecord
from watchable.providers.emby import EmbyClient
from watchable.providers.jellyfin import JellyfinClient
from watchable.providers.plex import PlexClient

_REGISTRY: dict[str, type[MediaServerClient]] = {
    "plex": PlexClient,
    "jellyfin": JellyfinClient,
    "emby": EmbyClient,
}


def get_client_class(server_type: str) -> type[MediaServerClient]:
    """Look up the provider client class for a config `type:` value."""
    try:
        return _REGISTRY[server_type]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown server type {server_type!r}. Known types: {known}") from exc


__all__ = [
    "MediaServerClient",
    "WatchStateRecord",
    "PlexClient",
    "JellyfinClient",
    "EmbyClient",
    "get_client_class",
]
