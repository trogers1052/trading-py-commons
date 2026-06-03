"""Tests for trading_commons.config — precedence and Docker secrets."""

from __future__ import annotations

import importlib
from typing import ClassVar

import pytest
from pydantic import BaseModel
from pydantic_settings import SettingsConfigDict

import trading_commons.config as config_mod
from trading_commons.config import BaseServiceSettings, read_secret


class DemoSettings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="DEMO_", env_file=None, extra="ignore")
    poll_interval: int = 30


# ---- nested-section settings (the risk-engine shape) ----------------------
class PositionSizing(BaseModel):
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 20.0


class Limits(BaseModel):
    daily_loss_pct: float = 5.0


class DeepInner(BaseModel):
    value: int = 1


class DeepMiddle(BaseModel):
    inner: DeepInner = DeepInner()


class RiskSettings(BaseServiceSettings):
    model_config = SettingsConfigDict(
        env_prefix="RISK_",
        env_file=None,
        extra="ignore",
        env_nested_delimiter="__",
    )
    position_sizing: PositionSizing = PositionSizing()
    limits: Limits = Limits()
    middle: DeepMiddle = DeepMiddle()


@pytest.fixture(autouse=True)
def _clean_risk_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("RISK_"):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Strip any DEMO_* env vars between tests for isolation.
    import os

    for key in list(os.environ):
        if key.startswith("DEMO_"):
            monkeypatch.delenv(key, raising=False)
    # Point secrets at a non-existent dir by default.
    monkeypatch.setattr(config_mod, "SECRETS_DIR", config_mod.Path("/nonexistent-secrets"))
    yield


def test_defaults_when_no_env_no_yaml():
    s = DemoSettings()
    assert s.redis_host == "localhost"
    assert s.redis_port == 6379
    assert s.poll_interval == 30


def test_redis_url_without_password():
    s = DemoSettings(redis_host="cache", redis_port=6380, redis_db=2)
    assert s.redis_url == "redis://cache:6380/2"


def test_redis_url_with_password():
    s = DemoSettings(redis_password="secret", redis_host="cache")
    assert s.redis_url == "redis://:secret@cache:6379/0"


def test_kafka_broker_list():
    s = DemoSettings(kafka_brokers="a:9092, b:9092 ,c:9092")
    assert s.kafka_broker_list == ["a:9092", "b:9092", "c:9092"]


def test_telegram_enabled_flag():
    assert DemoSettings().telegram_enabled is False
    assert DemoSettings(telegram_bot_token="t", telegram_chat_id="c").telegram_enabled is True


def test_yaml_overrides_defaults(tmp_path):
    yaml_file = tmp_path / "cfg.yaml"
    yaml_file.write_text("redis_host: yamlhost\npoll_interval: 99\n")
    s = DemoSettings.from_yaml(yaml_file)
    assert s.redis_host == "yamlhost"
    assert s.poll_interval == 99


def test_env_beats_yaml(tmp_path, monkeypatch):
    yaml_file = tmp_path / "cfg.yaml"
    yaml_file.write_text("redis_host: yamlhost\npoll_interval: 99\n")
    monkeypatch.setenv("DEMO_REDIS_HOST", "envhost")
    s = DemoSettings.from_yaml(yaml_file)
    # env wins over YAML (the CORRECT precedence)
    assert s.redis_host == "envhost"
    # YAML still wins over default for the field with no env var
    assert s.poll_interval == 99


def test_missing_yaml_file_is_ok():
    s = DemoSettings.from_yaml("/no/such/file.yaml")
    assert s.redis_host == "localhost"


def test_non_mapping_yaml_raises(tmp_path):
    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError):
        DemoSettings.from_yaml(yaml_file)


def test_read_secret_present(tmp_path):
    (tmp_path / "redis_password").write_text("  topsecret\n")
    assert read_secret("redis_password", secrets_dir=tmp_path) == "topsecret"


def test_read_secret_absent(tmp_path):
    assert read_secret("missing", secrets_dir=tmp_path) is None


def test_docker_secret_beats_env(tmp_path, monkeypatch):
    # Env says one thing, the mounted secret says another — secret must win.
    (tmp_path / "redis_password").write_text("from_secret")
    monkeypatch.setattr(config_mod, "SECRETS_DIR", tmp_path)
    monkeypatch.setenv("DEMO_REDIS_PASSWORD", "from_env")
    s = DemoSettings()
    assert s.redis_password == "from_secret"


def test_subclass_can_extend_secret_fields(tmp_path, monkeypatch):
    class WithApiKey(BaseServiceSettings):
        model_config = SettingsConfigDict(env_prefix="WAK_", env_file=None, extra="ignore")
        api_key: str | None = None
        SECRET_FIELDS: ClassVar[tuple[str, ...]] = ("api_key",)

    (tmp_path / "api_key").write_text("sekret-key")
    monkeypatch.setattr(config_mod, "SECRETS_DIR", tmp_path)
    s = WithApiKey()
    assert s.api_key == "sekret-key"


def test_module_reimport_keeps_secrets_dir_default():
    # Sanity: the module-level SECRETS_DIR default is /run/secrets.
    reloaded = importlib.reload(config_mod)
    assert str(reloaded.SECRETS_DIR) == "/run/secrets"


# ---- nested-section precedence -------------------------------------------
def test_nested_yaml_overrides_defaults(tmp_path):
    yaml_file = tmp_path / "risk.yaml"
    yaml_file.write_text("position_sizing:\n  risk_per_trade_pct: 2.5\n")
    s = RiskSettings.from_yaml(yaml_file)
    assert s.position_sizing.risk_per_trade_pct == 2.5
    # untouched nested default preserved
    assert s.position_sizing.max_position_pct == 20.0


def test_nested_env_beats_nested_yaml(tmp_path, monkeypatch):
    yaml_file = tmp_path / "risk.yaml"
    yaml_file.write_text("position_sizing:\n  risk_per_trade_pct: 2.5\n")
    monkeypatch.setenv("RISK_POSITION_SIZING__RISK_PER_TRADE_PCT", "0.5")
    s = RiskSettings.from_yaml(yaml_file)
    # env wins over the nested YAML value
    assert s.position_sizing.risk_per_trade_pct == 0.5


def test_nested_yaml_applies_to_sibling_when_other_env_set(tmp_path, monkeypatch):
    yaml_file = tmp_path / "risk.yaml"
    yaml_file.write_text(
        "position_sizing:\n" "  risk_per_trade_pct: 2.5\n" "  max_position_pct: 33.0\n"
    )
    # Only one nested field is shadowed by env.
    monkeypatch.setenv("RISK_POSITION_SIZING__RISK_PER_TRADE_PCT", "0.5")
    s = RiskSettings.from_yaml(yaml_file)
    assert s.position_sizing.risk_per_trade_pct == 0.5  # env wins
    assert s.position_sizing.max_position_pct == 33.0  # YAML still applies


def test_nested_env_for_one_section_does_not_affect_other(tmp_path, monkeypatch):
    yaml_file = tmp_path / "risk.yaml"
    yaml_file.write_text(
        "position_sizing:\n  risk_per_trade_pct: 2.5\n" "limits:\n  daily_loss_pct: 7.0\n"
    )
    monkeypatch.setenv("RISK_POSITION_SIZING__RISK_PER_TRADE_PCT", "0.5")
    s = RiskSettings.from_yaml(yaml_file)
    assert s.position_sizing.risk_per_trade_pct == 0.5
    # sibling nested section untouched — YAML applies
    assert s.limits.daily_loss_pct == 7.0


def test_deep_nesting_env_beats_yaml(tmp_path, monkeypatch):
    yaml_file = tmp_path / "risk.yaml"
    yaml_file.write_text("middle:\n  inner:\n    value: 42\n")
    monkeypatch.setenv("RISK_MIDDLE__INNER__VALUE", "99")
    s = RiskSettings.from_yaml(yaml_file)
    assert s.middle.inner.value == 99


def test_deep_nesting_yaml_applies_without_env(tmp_path):
    yaml_file = tmp_path / "risk.yaml"
    yaml_file.write_text("middle:\n  inner:\n    value: 42\n")
    s = RiskSettings.from_yaml(yaml_file)
    assert s.middle.inner.value == 42


def test_flat_field_on_nested_settings_still_env_beats_yaml(tmp_path, monkeypatch):
    # Flat top-level behaviour must be preserved exactly.
    yaml_file = tmp_path / "risk.yaml"
    yaml_file.write_text("redis_host: yamlhost\n")
    monkeypatch.setenv("RISK_REDIS_HOST", "envhost")
    s = RiskSettings.from_yaml(yaml_file)
    assert s.redis_host == "envhost"


def test_undeclared_yaml_key_is_ignored_not_treated_as_nested(tmp_path):
    # A YAML key that is not a declared field (extra="ignore") must not be
    # treated as a nested model; it is simply dropped by pydantic.
    yaml_file = tmp_path / "risk.yaml"
    yaml_file.write_text("not_a_field:\n  whatever: 1\nredis_host: yamlhost\n")
    s = RiskSettings.from_yaml(yaml_file)
    assert s.redis_host == "yamlhost"
