"""trading_commons — shared infrastructure for the trading platform's Python services.

Modules
-------
- ``config``   : ``BaseServiceSettings`` pydantic-settings base with Docker-secrets
                 support and an env > YAML > defaults precedence helper.
- ``metrics``  : Prometheus client re-export with a transparent no-op fallback.
- ``redisx``   : ``RedisBase`` with connect/reconnect/retry and a ``redis_url`` helper.
- ``telegram`` : sync + async Telegram client with retry and no-op-without-creds.
- ``daemon``   : ``run_daemon`` signal-aware run loop with optional metrics server.
"""

from .config import BaseServiceSettings, read_secret
from .daemon import run_daemon
from .redisx import RedisBase, redis_url
from .telegram import TelegramClient

__all__ = [
    "BaseServiceSettings",
    "read_secret",
    "RedisBase",
    "redis_url",
    "TelegramClient",
    "run_daemon",
]

__version__ = "0.1.0"
