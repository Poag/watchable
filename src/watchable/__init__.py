"""watchable: sync watch state between Plex, Jellyfin, and Emby via a local database."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("watchable")
except PackageNotFoundError:  # pragma: no cover - local/dev checkout
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
