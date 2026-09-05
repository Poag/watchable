"""YAML configuration loading and validation.

The whole app is configured from one YAML file (see ``config.example.yaml``
at the repo root). Nothing here talks to a network or a database -- this
module's only job is turning YAML text into validated, typed Python objects,
so config mistakes are caught before any sync work starts.
"""

from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from watchable.providers.base import SUPPORTED_MEDIA_TYPES, MediaType

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-(?P<default>[^}]*))?\}")


class ConfigError(ValueError):
    """Raised for any problem with the config file's content or structure."""


class Direction(str, Enum):
    ONE_WAY = "one-way"
    BIDIRECTIONAL = "bidirectional"


class ConflictStrategy(str, Enum):
    """How to pick a winner when both sides of a bidirectional pair changed.

    - most_watched: whichever side has progressed further (played, or a
      higher view offset) wins. Good default -- it never "rewinds" progress.
    - latest: whichever side was updated most recently (by the server's own
      last-played timestamp) wins.
    - source_wins / target_wins: fixed priority, useful when one server is
      considered the source of truth even in a bidirectional pair.
    """

    MOST_WATCHED = "most_watched"
    LATEST = "latest"
    SOURCE_WINS = "source_wins"
    TARGET_WINS = "target_wins"


def _interpolate_env(value: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` references against os.environ."""

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group("default")
        if var_name in os.environ:
            return os.environ[var_name]
        if default is not None:
            return default
        raise ConfigError(
            f"Config references environment variable ${{{var_name}}} which is not set "
            "and has no default (use ${VAR:-fallback} to supply one)."
        )

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _walk_interpolate(obj: Any) -> Any:
    if isinstance(obj, str):
        return _interpolate_env(obj)
    if isinstance(obj, dict):
        return {k: _walk_interpolate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_interpolate(v) for v in obj]
    return obj


class ServerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    key: str = Field(description="Internal identifier used to reference this server elsewhere")
    type: str
    url: str
    token: str | None = None
    api_key: str | None = None
    verify_tls: bool = True
    timeout_seconds: float = 15.0

    @model_validator(mode="after")
    def _check_credentials(self) -> ServerConfig:
        if self.type == "plex" and not self.token:
            raise ConfigError(f"Server {self.key!r}: plex servers require `token`")
        if self.type in ("jellyfin", "emby") and not self.api_key:
            raise ConfigError(f"Server {self.key!r}: {self.type} servers require `api_key`")
        if self.type not in ("plex", "jellyfin", "emby"):
            raise ConfigError(
                f"Server {self.key!r}: unknown type {self.type!r} "
                "(expected plex, jellyfin, or emby)"
            )
        return self


class UserConfig(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    accounts: dict[str, str] = Field(
        description="Maps server key -> that server's account identifier for this person"
    )


class SyncPairConfig(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    source: str
    target: str
    direction: Direction = Direction.ONE_WAY
    media_types: list[MediaType] = Field(default_factory=lambda: list(SUPPORTED_MEDIA_TYPES))
    conflict_strategy: ConflictStrategy | None = Field(
        default=None,
        description="Overrides the global sync.conflict_strategy for this pair only",
    )

    @field_validator("media_types")
    @classmethod
    def _check_media_types(cls, value: list[str]) -> list[str]:
        bad = [m for m in value if m not in SUPPORTED_MEDIA_TYPES]
        if bad:
            raise ConfigError(
                f"Unsupported media_types {bad}; expected one of {list(SUPPORTED_MEDIA_TYPES)}"
            )
        return value


class ScheduleConfig(BaseModel):
    model_config = {"extra": "forbid"}

    interval_minutes: int | None = Field(
        default=None, description="If set, `watchable run` loops forever, syncing on this cadence"
    )


class SyncConfig(BaseModel):
    model_config = {"extra": "forbid"}

    conflict_strategy: ConflictStrategy = ConflictStrategy.MOST_WATCHED
    pairs: list[SyncPairConfig]
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)

    @field_validator("pairs")
    @classmethod
    def _check_pairs_nonempty(cls, value: list[SyncPairConfig]) -> list[SyncPairConfig]:
        if not value:
            raise ConfigError("sync.pairs must contain at least one entry")
        return value


class DatabaseConfig(BaseModel):
    model_config = {"extra": "forbid"}

    path: str = "./data/watchable.db"


class AppConfig(BaseModel):
    model_config = {"extra": "forbid"}

    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    log_level: str = "INFO"
    servers: list[ServerConfig]
    users: list[UserConfig] = Field(default_factory=list)
    sync: SyncConfig

    @model_validator(mode="after")
    def _cross_check(self) -> AppConfig:
        server_keys = {s.key for s in self.servers}
        if len(server_keys) != len(self.servers):
            raise ConfigError("servers[].key values must be unique")

        for user in self.users:
            for server_key in user.accounts:
                if server_key not in server_keys:
                    raise ConfigError(
                        f"User {user.name!r} references unknown server {server_key!r}"
                    )

        for pair in self.sync.pairs:
            for role, key in (("source", pair.source), ("target", pair.target)):
                if key not in server_keys:
                    raise ConfigError(
                        f"Sync pair {pair.name!r}: {role} {key!r} is not a defined server"
                    )
            if pair.source == pair.target:
                raise ConfigError(f"Sync pair {pair.name!r}: source and target must differ")

        return self

    def server_map(self) -> dict[str, ServerConfig]:
        return {s.key: s for s in self.servers}


def load_config(path: str | Path) -> AppConfig:
    """Load, env-interpolate, and validate a YAML config file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    raw = _walk_interpolate(raw)

    # `servers` is authored as a mapping (server_key: {...}) for readability;
    # fold the key into each entry before handing off to pydantic.
    servers_raw = raw.get("servers", {})
    if not isinstance(servers_raw, dict):
        raise ConfigError("`servers` must be a mapping of server_key -> server config")
    raw["servers"] = [{"key": key, **value} for key, value in servers_raw.items()]

    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:  # pydantic wraps our ConfigError validators in ValidationError
        raise ConfigError(f"Invalid configuration: {exc}") from exc
