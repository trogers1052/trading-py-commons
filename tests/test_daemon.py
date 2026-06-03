"""Tests for trading_commons.daemon — loop runs and shuts down cleanly."""

from __future__ import annotations

import signal
import threading
from unittest.mock import MagicMock

from trading_commons.daemon import run_daemon


def test_runs_loop_and_shuts_down_after_n_iterations():
    event = threading.Event()
    calls = {"setup": 0, "loop": 0, "teardown": 0}

    def setup():
        calls["setup"] += 1

    def loop_once():
        calls["loop"] += 1
        if calls["loop"] >= 3:
            event.set()  # request shutdown after 3 iterations

    def teardown():
        calls["teardown"] += 1

    run_daemon(
        setup,
        loop_once,
        teardown,
        interval=0,
        _shutdown_event=event,
        _install_signals=False,
    )

    assert calls["setup"] == 1
    assert calls["loop"] >= 3
    assert calls["teardown"] == 1


def test_teardown_runs_even_if_setup_raises():
    event = threading.Event()
    teardown = MagicMock()

    def setup():
        raise RuntimeError("setup boom")

    try:
        run_daemon(
            setup,
            lambda: None,
            teardown,
            interval=0,
            _shutdown_event=event,
            _install_signals=False,
        )
    except RuntimeError:
        pass
    teardown.assert_called_once()


def test_loop_exception_does_not_kill_daemon():
    event = threading.Event()
    calls = {"loop": 0}

    def loop_once():
        calls["loop"] += 1
        if calls["loop"] == 1:
            raise ValueError("transient")
        if calls["loop"] >= 3:
            event.set()

    run_daemon(
        None,
        loop_once,
        None,
        interval=0,
        _shutdown_event=event,
        _install_signals=False,
    )
    assert calls["loop"] >= 3  # kept going past the exception


def test_pre_set_event_skips_loop_body():
    event = threading.Event()
    event.set()  # already shut down before we start
    loop = MagicMock()
    run_daemon(None, loop, None, interval=0, _shutdown_event=event, _install_signals=False)
    loop.assert_not_called()


def test_starts_metrics_server_when_http_port_given(monkeypatch):
    event = threading.Event()
    event.set()
    started = {}
    monkeypatch.setattr(
        "trading_commons.daemon.start_metrics_server",
        lambda port: started.setdefault("port", port),
    )
    run_daemon(
        None, lambda: None, None, http_port=9099, _shutdown_event=event, _install_signals=False
    )
    assert started["port"] == 9099


def test_no_metrics_server_when_port_none(monkeypatch):
    event = threading.Event()
    event.set()
    spy = MagicMock()
    monkeypatch.setattr("trading_commons.daemon.start_metrics_server", spy)
    run_daemon(
        None, lambda: None, None, http_port=None, _shutdown_event=event, _install_signals=False
    )
    spy.assert_not_called()


def test_signal_handler_sets_shutdown(monkeypatch):
    # Capture the handler registered for SIGTERM, then invoke it.
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))

    event = threading.Event()
    calls = {"loop": 0}

    def loop_once():
        calls["loop"] += 1
        # Simulate a SIGTERM arriving during the first iteration.
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    run_daemon(None, loop_once, None, interval=0, _shutdown_event=event, _install_signals=True)

    assert signal.SIGINT in handlers
    assert signal.SIGTERM in handlers
    assert event.is_set()
    assert calls["loop"] == 1  # loop exited right after the signal
