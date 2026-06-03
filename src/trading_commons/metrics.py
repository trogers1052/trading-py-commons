"""Prometheus metrics with a transparent no-op fallback.

``prometheus_client`` is an *optional* dependency. When it is installed this
module re-exports the real ``Counter`` / ``Gauge`` / ``Histogram`` /
``start_http_server``. When it is **not** installed, drop-in no-op stand-ins
are exposed so that callsites can call ``.inc()`` / ``.set()`` / ``.observe()``
/ ``.labels()`` unconditionally without guarding every call.

This consolidates the shim duplicated across decision-engine,
reporting-service, analytics, and stop-loss-guardian.

Usage::

    from trading_commons.metrics import Counter, start_metrics_server

    SIGNALS = Counter("signals_total", "Signals produced", ["type"])
    SIGNALS.labels(type="BUY").inc()

    start_metrics_server(port=9093)   # no-op if prometheus_client absent
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 9090

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        start_http_server,
    )

    HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover - exercised via test monkeypatch
    HAS_PROMETHEUS = False

    class _NoOpMetric:
        """No-op metric that silently absorbs any call. Zero-cost."""

        def inc(self, amount: float = 1) -> None:
            pass

        def dec(self, amount: float = 1) -> None:
            pass

        def set(self, value: float) -> None:
            pass

        def observe(self, value: float) -> None:
            pass

        def labels(self, *args: object, **kwargs: object) -> _NoOpMetric:
            return self

    class _NoOpFactory:
        """Creates _NoOpMetric instances, accepting prometheus_client's args."""

        def __call__(self, *args: object, **kwargs: object) -> _NoOpMetric:
            return _NoOpMetric()

    Counter = _NoOpFactory()  # type: ignore[assignment,misc]
    Gauge = _NoOpFactory()  # type: ignore[assignment,misc]
    Histogram = _NoOpFactory()  # type: ignore[assignment,misc]

    def start_http_server(port: int, *args: object, **kwargs: object) -> None:  # type: ignore[misc]
        pass


def start_metrics_server(port: int | None = None) -> bool:
    """Start the Prometheus metrics HTTP server.

    Port resolution: explicit ``port`` arg, else the ``METRICS_PORT`` env
    var, else :data:`_DEFAULT_PORT` (9090).

    Returns ``True`` if a real server was started, ``False`` if
    prometheus_client is absent or the server failed to start (logged, never
    raised — metrics must never crash a service).
    """
    if not HAS_PROMETHEUS:
        logger.warning("prometheus_client not installed — metrics endpoint disabled")
        return False
    resolved = port if port is not None else int(os.environ.get("METRICS_PORT", str(_DEFAULT_PORT)))
    try:
        start_http_server(resolved)
    except Exception as exc:  # noqa: BLE001 - never crash on metrics
        logger.warning("metrics server failed to start: %s", exc)
        return False
    logger.info("Metrics server listening on :%d/metrics", resolved)
    return True


__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "start_http_server",
    "start_metrics_server",
    "HAS_PROMETHEUS",
]
