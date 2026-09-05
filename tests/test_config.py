from __future__ import annotations

import pytest

from watchable.config import ConfigError, Direction, load_config

VALID_YAML = """
database:
  path: ./data/watchable.db
servers:
  plex_main:
    type: plex
    url: http://plex.lan:32400
    token: ${TEST_PLEX_TOKEN}
  jellyfin_main:
    type: jellyfin
    url: http://jf.lan:8096
    api_key: ${TEST_JF_KEY:-fallback-key}
users:
  - name: alex
    accounts:
      plex_main: sometoken
      jellyfin_main: someuserid
sync:
  pairs:
    - name: p2j
      source: plex_main
      target: jellyfin_main
      direction: one-way
"""


def test_load_valid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PLEX_TOKEN", "abc123")
    monkeypatch.delenv("TEST_JF_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(VALID_YAML)

    config = load_config(path)

    assert len(config.servers) == 2
    plex = config.server_map()["plex_main"]
    assert plex.token == "abc123"
    jellyfin = config.server_map()["jellyfin_main"]
    assert jellyfin.api_key == "fallback-key"  # ${VAR:-default} fell back
    assert config.sync.pairs[0].direction == Direction.ONE_WAY


def test_missing_env_var_without_default_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_PLEX_TOKEN", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(VALID_YAML)

    with pytest.raises(ConfigError, match="TEST_PLEX_TOKEN"):
        load_config(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_unknown_server_type_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PLEX_TOKEN", "abc123")
    bad = VALID_YAML.replace("type: jellyfin", "type: netflix")
    path = tmp_path / "config.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError):
        load_config(path)


def test_plex_without_token_rejected(tmp_path):
    bad = """
servers:
  plex_main:
    type: plex
    url: http://plex.lan:32400
sync:
  pairs:
    - name: p
      source: plex_main
      target: plex_main
"""
    path = tmp_path / "config.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError):
        load_config(path)


def test_pair_referencing_unknown_server_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PLEX_TOKEN", "abc123")
    bad = VALID_YAML.replace("target: jellyfin_main", "target: nonexistent")
    path = tmp_path / "config.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError, match="nonexistent"):
        load_config(path)


def test_pair_source_equals_target_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PLEX_TOKEN", "abc123")
    bad = VALID_YAML.replace("target: jellyfin_main", "target: plex_main")
    path = tmp_path / "config.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError, match="must differ"):
        load_config(path)


def test_empty_pairs_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PLEX_TOKEN", "abc123")
    bad = VALID_YAML.split("sync:")[0] + "sync:\n  pairs: []\n"
    path = tmp_path / "config.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError):
        load_config(path)


def test_backup_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PLEX_TOKEN", "abc123")
    path = tmp_path / "config.yaml"
    path.write_text(VALID_YAML)

    config = load_config(path)

    assert config.backup.dir == "./data/backups"
    assert config.backup.interval_hours is None
    assert config.backup.keep_days == 30


def test_backup_interval_hours_must_be_positive(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PLEX_TOKEN", "abc123")
    bad = VALID_YAML + "backup:\n  interval_hours: 0\n"
    path = tmp_path / "config.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError, match="interval_hours"):
        load_config(path)


def test_backup_keep_days_must_be_positive(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PLEX_TOKEN", "abc123")
    bad = VALID_YAML + "backup:\n  keep_days: -1\n"
    path = tmp_path / "config.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError, match="keep_days"):
        load_config(path)


def test_backup_keep_days_null_disables_purging(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PLEX_TOKEN", "abc123")
    yaml_text = VALID_YAML + "backup:\n  keep_days: null\n  interval_hours: 6\n"
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text)

    config = load_config(path)

    assert config.backup.keep_days is None
    assert config.backup.interval_hours == 6


def test_duplicate_server_keys_rejected():
    # servers is authored as a mapping in YAML, so duplicate keys are a YAML-level
    # concept (last one wins) -- this instead checks the safety net in AppConfig
    # in case servers is ever constructed programmatically with a collision.
    # Direct construction (bypassing load_config) surfaces pydantic's own
    # ValidationError rather than load_config's re-wrapped ConfigError.
    import pydantic

    from watchable.config import AppConfig, ServerConfig, SyncConfig, SyncPairConfig

    with pytest.raises(pydantic.ValidationError, match="unique"):
        AppConfig(
            servers=[
                ServerConfig(key="dup", type="jellyfin", url="http://a", api_key="k"),
                ServerConfig(key="dup", type="emby", url="http://b", api_key="k"),
            ],
            sync=SyncConfig(pairs=[SyncPairConfig(name="p", source="dup", target="dup2")]),
        )
