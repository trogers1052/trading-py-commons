# trading-py-commons

Shared Python infrastructure for the trading platform's Python services
(decision-engine, reporting-service, analytics, context-service, risk-engine,
robinhood-sync, stop-loss-guardian, …).

This package consolidates the boilerplate that had been copy-pasted across
those services — settings, a Prometheus shim, Redis connection plumbing, a
Telegram client, and a daemon run loop — into one tested, documented library.

- **Import name:** `trading_commons`
- **Distribution name:** `trading-py-commons`
- **Python:** ≥ 3.11

```bash
pip install "trading-py-commons @ git+https://github.com/trogers1052/trading-py-commons.git@v0.1.0"
```

To also pull in real Prometheus metrics (otherwise `metrics` runs as no-ops):

```bash
pip install "trading-py-commons[prometheus] @ git+https://github.com/trogers1052/trading-py-commons.git@v0.1.0"
```

---

## Modules

### `config` — `BaseServiceSettings`

A `pydantic_settings.BaseSettings` (pydantic v2 / `SettingsConfigDict`) base
class carrying the blocks every service shares: Kafka brokers/group, Redis
host/port/db/password, Telegram token/chat_id, log level. Subclass it and add
your own fields.

It provides:

- **Docker-secrets support** — a `model_validator` reads `/run/secrets/<name>`
  and *prefers it over the environment*, so a mounted secret always wins.
  Extend `SECRET_FIELDS` to cover your own sensitive fields.
- **`redis_url` property** — assembles the connection URL (with password).
- **`from_yaml()` precedence helper** — enforces the **correct** precedence:
  **env > YAML > defaults**. (Fixes the bug where YAML silently shadowed env
  vars — see CLAUDE.md §6.)

```python
from typing import ClassVar
from pydantic_settings import SettingsConfigDict
from trading_commons.config import BaseServiceSettings


class MySettings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="MYSVC_")

    poll_interval: int = 30
    api_key: str | None = None
    # Allow api_key to be supplied as a Docker secret too:
    SECRET_FIELDS: ClassVar[tuple[str, ...]] = (
        *BaseServiceSettings.SECRET_FIELDS,
        "api_key",
    )


# env > YAML > defaults
settings = MySettings.from_yaml("config/mysvc.yaml")
print(settings.redis_url)            # redis://localhost:6379/0
print(settings.kafka_broker_list)    # ["localhost:19092"]
```

### `metrics` — Prometheus shim with no-op fallback

`prometheus_client` is optional. When installed, `Counter` / `Gauge` /
`Histogram` / `start_http_server` are the real thing. When missing, drop-in
no-ops are exposed so callsites can call `.inc()` / `.set()` / `.observe()` /
`.labels()` unconditionally — no guards needed.

```python
from trading_commons.metrics import Counter, start_metrics_server

SIGNALS = Counter("signals_total", "Signals produced", ["type"])
SIGNALS.labels(type="BUY").inc()

# No-op if prometheus_client is not installed; returns True/False.
start_metrics_server(port=9093)
```

### `redisx` — `RedisBase`

A Redis connection base with `connect()` / `reconnect()` / `_with_retry()`
(exponential backoff), socket timeouts, and a `redis_url` helper. Subclass it
and wrap each Redis call in `self._with_retry(...)`.

```python
from trading_commons.redisx import RedisBase, redis_url


class SyncedOrders(RedisBase):
    KEY = "robinhood:synced_orders"

    def is_synced(self, order_id: str) -> bool:
        return self._with_retry(self.client.sismember, self.KEY, order_id)

    def mark_synced(self, order_id: str) -> None:
        self._with_retry(self.client.sadd, self.KEY, order_id)


store = SyncedOrders(host="localhost", port=6379, db=0)
store.connect()
store.mark_synced("abc-123")

print(redis_url("cache", 6380, 1, "pw"))  # redis://:pw@cache:6380/1
```

### `telegram` — `TelegramClient`

An httpx-based client with **sync and async** `send_message`, HTML parse mode,
exponential-backoff retry, and a graceful no-op when no token/chat_id is
configured (returns `False`, never raises).

```python
from trading_commons.telegram import TelegramClient

tg = TelegramClient(bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id)

# sync
tg.send_message("<b>Stop hit</b> on AAPL @ 178.50")

# async
await tg.send_message_async("Queued setup ready: PLTR")

# Unconfigured client is a safe no-op:
TelegramClient(None, None).send_message("ignored")  # -> False
```

### `daemon` — `run_daemon`

A signal-aware run loop: installs SIGINT/SIGTERM handlers + a shutdown flag,
optionally starts the metrics HTTP server, runs `loop_once()` every `interval`
seconds until shutdown, and always calls `teardown()`. The inter-iteration
sleep is interruptible — a signal wakes the loop immediately. A failing
iteration is logged and the loop continues.

```python
from trading_commons.daemon import run_daemon

store = ...  # e.g. a RedisBase subclass

def setup():
    store.connect()

def loop_once():
    analyze_once()

def teardown():
    store.close()

run_daemon(setup, loop_once, teardown, interval=300, http_port=9099)
```

---

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

pytest          # tests + coverage (≥ 80%)
ruff check .    # lint
black --check . # format check
mypy            # type check
```

CI (`.github/workflows/ci.yml`) runs ruff, black, mypy, and pytest on Python
3.11 and 3.12 for every push and PR to `main`. This is a library — no Docker
image is built.

---

## Built with Claude Code

A large portion of this project — implementation, tests, and documentation — was written in pair-programming sessions with [Claude Code](https://claude.com/claude-code), Anthropic's agentic command-line tool.
