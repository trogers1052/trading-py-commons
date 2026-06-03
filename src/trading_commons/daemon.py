"""Signal-aware daemon run loop.

:func:`run_daemon` consolidates the SIGINT/SIGTERM + shutdown-flag + sleep
loop duplicated across the platform's long-running Python services. It:

1. installs SIGINT/SIGTERM handlers that flip a shutdown flag,
2. optionally starts the Prometheus metrics HTTP server,
3. runs ``setup()`` once,
4. calls ``loop_once()`` every ``interval`` seconds until shutdown,
5. always calls ``teardown()`` in a ``finally`` block.

The sleep between iterations is interruptible — a signal wakes the loop
immediately rather than waiting out the full interval.

Usage::

    from trading_commons.daemon import run_daemon

    def setup():    store.connect()
    def loop_once(): analyze()
    def teardown(): store.close()

    run_daemon(setup, loop_once, teardown, interval=300, http_port=9099)
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable

from .metrics import start_metrics_server

logger = logging.getLogger(__name__)

# Type alias for the optional lifecycle callbacks.
_Callback = Callable[[], None]


def run_daemon(
    setup: _Callback | None,
    loop_once: _Callback,
    teardown: _Callback | None = None,
    *,
    interval: float = 60.0,
    http_port: int | None = None,
    _shutdown_event: threading.Event | None = None,
    _install_signals: bool = True,
) -> None:
    """Run ``loop_once`` every ``interval`` seconds until a shutdown signal.

    Parameters
    ----------
    setup:
        Called once before the loop starts. May be ``None``.
    loop_once:
        Called once per iteration. Exceptions are logged and the loop
        continues — one bad iteration must not kill the daemon.
    teardown:
        Called once after the loop ends (always, via ``finally``). May be
        ``None``.
    interval:
        Seconds between iterations. The wait is interruptible by a signal.
    http_port:
        If set, starts the Prometheus metrics HTTP server on this port
        (no-op if prometheus_client is not installed).
    _shutdown_event, _install_signals:
        Test seams. Pass a pre-made event and/or skip signal installation
        (signals can only be installed from the main thread).
    """
    shutdown = _shutdown_event if _shutdown_event is not None else threading.Event()

    if _install_signals:

        def _handle(signum: int, _frame: object) -> None:
            logger.info("Received signal %s, shutting down...", signum)
            shutdown.set()

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

    if http_port is not None:
        start_metrics_server(http_port)

    try:
        if setup is not None:
            setup()

        logger.info("Daemon started (interval=%ss)", interval)
        while not shutdown.is_set():
            try:
                loop_once()
            except Exception:  # noqa: BLE001 - one bad iteration must not crash the daemon
                logger.exception("Daemon iteration failed; continuing")
            # Interruptible wait: returns immediately when shutdown is set.
            shutdown.wait(timeout=interval)
    finally:
        logger.info("Daemon shutting down")
        if teardown is not None:
            try:
                teardown()
            except Exception:  # noqa: BLE001
                logger.exception("Daemon teardown failed")


__all__ = ["run_daemon"]
