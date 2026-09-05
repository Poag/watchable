"""Wiring: turn a loaded AppConfig into live provider clients and a database.

Kept separate from watchable.cli so tests (and any future entry point, e.g.
a web UI) can build a working engine without going through click.
"""

from __future__ import annotations

import logging
from pathlib import Path

from watchable.config import AppConfig, ServerConfig
from watchable.db import Database
from watchable.providers.base import MediaServerClient
from watchable.providers.emby import EmbyClient
from watchable.providers.jellyfin import JellyfinClient
from watchable.providers.plex import PlexClient


def build_clients(config: AppConfig) -> dict[str, MediaServerClient]:
    return {server.key: _build_client(server) for server in config.servers}


def _build_client(server: ServerConfig) -> MediaServerClient:
    # Config validation already guarantees token/api_key are set for the
    # matching type (see ServerConfig._check_credentials), and restricts
    # `type` to one of these three -- so branching here (rather than the
    # generic providers.get_client_class registry) keeps each constructor
    # call's keyword arguments checkable by mypy.
    if server.type == "plex":
        assert server.token is not None
        return PlexClient(
            name=server.key, base_url=server.url, token=server.token,
            verify_tls=server.verify_tls, timeout=server.timeout_seconds,
        )
    assert server.api_key is not None
    cls = JellyfinClient if server.type == "jellyfin" else EmbyClient
    return cls(
        name=server.key, base_url=server.url, api_key=server.api_key,
        verify_tls=server.verify_tls, timeout=server.timeout_seconds,
    )


def open_database(config: AppConfig) -> Database:
    return Database(config.database.path)


def database_path(config: AppConfig) -> Path:
    return Path(config.database.path)


def backup_dir_path(config: AppConfig) -> Path:
    return Path(config.backup.dir)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
