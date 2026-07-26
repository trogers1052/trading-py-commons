"""trading_commons — shared infrastructure for the trading platform's Python services.

Modules
-------
- ``clock``    : the time source a service reads "now" from — real by default,
                 simulated under ``CLOCK_MODE=replay`` for whole-system replays.
- ``config``   : ``BaseServiceSettings`` pydantic-settings base with Docker-secrets
                 support and an env > YAML > defaults precedence helper (nested-aware).
- ``metrics``  : Prometheus client re-export with a transparent no-op fallback.
- ``redisx``   : ``RedisBase`` with connect/reconnect/retry and a ``redis_url`` helper.
- ``telegram`` : sync + async Telegram client with retry and no-op-without-creds.
- ``daemon``   : ``run_daemon`` signal-aware run loop with optional metrics server.

Lazy submodule imports
----------------------
Submodules are imported **lazily** on first access (PEP 562 module
``__getattr__``). Importing one module — or one of the top-level names below —
pulls in only that module's third-party dependencies. For example::

    import trading_commons.telegram        # needs only httpx
    from trading_commons import RedisBase  # needs only redis

does not import ``config`` (and therefore not pydantic-settings or PyYAML).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.3.0"

# Map each lazily-exported top-level name to the submodule that defines it.
_LAZY_EXPORTS: dict[str, str] = {
    "BaseServiceSettings": "config",
    "read_secret": "config",
    "RedisBase": "redisx",
    "redis_url": "redisx",
    "TelegramClient": "telegram",
    "run_daemon": "daemon",
    "Clock": "clock",
    "SystemClock": "clock",
    "ManualClock": "clock",
}

# Submodules importable via attribute access (e.g. ``trading_commons.config``).
_SUBMODULES = ("config", "metrics", "redisx", "telegram", "daemon", "clock")

__all__ = [
    *_LAZY_EXPORTS.keys(),
    *_SUBMODULES,
    "__version__",
]


def __getattr__(name: str) -> Any:
    """Lazily import submodules / their public names on first access (PEP 562)."""
    if name in _LAZY_EXPORTS:
        module = importlib.import_module(f".{_LAZY_EXPORTS[name]}", __name__)
        value = getattr(module, name)
        globals()[name] = value  # cache for subsequent lookups
        return value
    if name in _SUBMODULES:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # pragma: no cover - import-time aid for type checkers only
    from .clock import Clock as Clock
    from .clock import ManualClock as ManualClock
    from .clock import SystemClock as SystemClock
    from .config import BaseServiceSettings as BaseServiceSettings
    from .config import read_secret as read_secret
    from .daemon import run_daemon as run_daemon
    from .redisx import RedisBase as RedisBase
    from .redisx import redis_url as redis_url
    from .telegram import TelegramClient as TelegramClient
