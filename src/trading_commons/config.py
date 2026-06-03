"""Configuration base for trading-platform Python services.

Provides :class:`BaseServiceSettings`, a ``pydantic_settings.BaseSettings``
subclass carrying the configuration blocks shared by every service (Kafka,
Redis, Telegram), plus:

- **Docker-secrets support** — a ``model_validator`` reads ``/run/secrets/<name>``
  and *prefers it over the environment* (so a mounted secret always wins).
  Generalised from ``robinhood-sync``'s ``_read_secret`` / ``_load_docker_secrets``.
- **A ``redis_url`` property** — assembles the Redis connection URL.
- **A documented precedence helper** — :meth:`BaseServiceSettings.from_yaml`
  enforces the *correct* precedence: **env > YAML > defaults**. Generalised
  from ``risk-engine`` / ``reporting-service``'s corrected ``from_yaml`` logic
  (env vars merged on top of the YAML dict so the environment always wins).

Services subclass :class:`BaseServiceSettings` and add their own fields::

    class MySettings(BaseServiceSettings):
        model_config = SettingsConfigDict(env_prefix="MYSVC_")
        poll_interval: int = 30

    settings = MySettings.from_yaml("config/mysvc.yaml")
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Directory Docker mounts secrets into. Overridable for tests.
SECRETS_DIR = Path("/run/secrets")


def read_secret(name: str, secrets_dir: Path | None = None) -> str | None:
    """Read a Docker secret from ``<secrets_dir>/<name>``.

    Returns the stripped file contents, or ``None`` if the secret file is
    absent. Defaults to :data:`SECRETS_DIR` (``/run/secrets``).
    """
    base = secrets_dir if secrets_dir is not None else SECRETS_DIR
    path = base / name
    try:
        if path.is_file():
            return path.read_text().strip()
    except OSError:
        return None
    return None


class BaseServiceSettings(BaseSettings):
    """Common settings base for all platform Python services.

    Carries the Kafka / Redis / Telegram blocks shared across services.
    Subclass it and add service-specific fields. Override ``model_config``
    (e.g. ``env_prefix``) as needed — the secrets and precedence behaviour
    is inherited.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Kafka -----------------------------------------------------------
    kafka_brokers: str = Field(
        default="localhost:19092", description="Comma-separated Kafka broker addresses"
    )
    kafka_group_id: str | None = Field(default=None, description="Kafka consumer group id")

    # ---- Redis -----------------------------------------------------------
    redis_host: str = Field(default="localhost", description="Redis host")
    redis_port: int = Field(default=6379, description="Redis port")
    redis_db: int = Field(default=0, description="Redis database number")
    redis_password: str | None = Field(default=None, description="Redis password (optional)")

    # ---- Telegram --------------------------------------------------------
    telegram_bot_token: str | None = Field(
        default=None, description="Telegram bot token (optional)"
    )
    telegram_chat_id: str | None = Field(default=None, description="Telegram chat id (optional)")

    # ---- Logging ---------------------------------------------------------
    log_level: str = Field(default="INFO", description="Root log level")

    # Field names that may be supplied via Docker secrets. Subclasses can
    # extend this with their own sensitive fields.
    SECRET_FIELDS: ClassVar[tuple[str, ...]] = (
        "redis_password",
        "telegram_bot_token",
        "telegram_chat_id",
    )

    @model_validator(mode="before")
    @classmethod
    def _load_docker_secrets(cls, values: Any) -> Any:
        """Prefer Docker secrets (``/run/secrets/<name>``) over env vars.

        Runs *before* validation so a mounted secret overrides any value
        coming from the environment or ``.env``.
        """
        if not isinstance(values, dict):
            return values
        secret_fields = getattr(cls, "SECRET_FIELDS", ())
        for field in secret_fields:
            secret_val = read_secret(field)
            if secret_val is not None:
                values[field] = secret_val
        return values

    # ---- Derived helpers -------------------------------------------------
    @property
    def kafka_broker_list(self) -> list[str]:
        """Return the Kafka brokers as a list."""
        return [b.strip() for b in self.kafka_brokers.split(",") if b.strip()]

    @property
    def redis_url(self) -> str:
        """Assemble the Redis connection URL (includes password if set)."""
        if self.redis_password:
            return (
                f"redis://:{self.redis_password}@"
                f"{self.redis_host}:{self.redis_port}/{self.redis_db}"
            )
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def telegram_enabled(self) -> bool:
        """True when both a bot token and chat id are configured."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    # ---- env > YAML > defaults precedence --------------------------------
    @classmethod
    def from_yaml(cls, yaml_path: str | os.PathLike[str]):
        """Load settings with **env > YAML > defaults** precedence.

        Precedence (highest first):

        1. Environment variables / ``.env`` (pydantic-settings sources)
        2. Values from the YAML file at ``yaml_path``
        3. Field defaults

        How it works: the YAML file is read into a dict and passed as
        constructor kwargs, which seeds the values. Then, for every key
        that has a matching environment variable present, that YAML value
        is dropped *so the env source wins* — the environment always takes
        precedence. This corrects the common bug where YAML values silently
        shadow env vars (the precedence bug noted in CLAUDE.md §6).

        **Nested sections** are handled too: when a field is a nested
        pydantic sub-model (e.g. a ``position_sizing`` block), a YAML value
        nested under it is dropped when the corresponding *nested* env var
        is set. The nested env var name is built from ``env_prefix`` plus
        the field path joined by ``env_nested_delimiter`` (default ``__``),
        upper-cased — e.g. ``RISK_POSITION_SIZING__RISK_PER_TRADE_PCT``.
        Recursion is arbitrary-depth, so deeply nested blocks work too.
        Flat top-level behaviour is unchanged.

        A missing YAML file is fine — settings load from env + defaults.
        """
        config_dict: dict[str, Any] = {}
        path = Path(yaml_path)
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                config_dict = yaml.safe_load(f) or {}
            if not isinstance(config_dict, dict):
                raise ValueError(f"YAML config at {path} must be a mapping")

        env_prefix = cls.model_config.get("env_prefix", "") or ""
        delimiter = cls.model_config.get("env_nested_delimiter") or "__"
        cls._drop_env_shadowed(config_dict, cls, env_prefix, delimiter, ())

        return cls(**config_dict)

    @staticmethod
    def _nested_model_for(model: Any, field_name: str) -> Any | None:
        """Return the nested ``BaseModel`` subclass for ``field_name``, if any.

        Looks at the field's declared annotation on ``model`` and returns the
        annotated type when it is a pydantic ``BaseModel`` subclass (the marker
        of a nested settings section). Returns ``None`` for plain scalar fields
        or unknown field names.
        """
        from pydantic import BaseModel

        fields = getattr(model, "model_fields", None)
        if not fields or field_name not in fields:
            return None
        annotation = fields[field_name].annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
        return None

    @classmethod
    def _drop_env_shadowed(
        cls,
        config_dict: dict[str, Any],
        model: Any,
        env_prefix: str,
        delimiter: str,
        path: tuple[str, ...],
    ) -> None:
        """Recursively drop YAML keys shadowed by a matching env var.

        For each key in ``config_dict``: build the env var name from
        ``env_prefix`` + the field path joined by ``delimiter`` (upper-cased).
        If that env var is set, the key is removed so pydantic-settings' env
        source wins. If the key maps to a nested pydantic sub-model and its
        value is a mapping, recurse into it (the parent key is kept so its
        non-shadowed siblings still apply).
        """
        for field_name in list(config_dict):
            full_path = (*path, field_name)
            env_key = (env_prefix + delimiter.join(full_path)).upper()
            if env_key in os.environ:
                del config_dict[field_name]
                continue
            nested_model = cls._nested_model_for(model, field_name)
            value = config_dict[field_name]
            if nested_model is not None and isinstance(value, dict):
                cls._drop_env_shadowed(value, nested_model, env_prefix, delimiter, full_path)
