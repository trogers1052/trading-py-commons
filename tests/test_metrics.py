"""Tests for trading_commons.metrics — both prometheus present and absent."""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest


def _reload_metrics():
    import trading_commons.metrics as m

    return importlib.reload(m)


@pytest.fixture
def metrics_present():
    """Reload the metrics module with prometheus_client available."""
    pytest.importorskip("prometheus_client")
    yield _reload_metrics()
    _reload_metrics()  # restore clean state


@pytest.fixture
def metrics_absent(monkeypatch):
    """Reload the metrics module simulating prometheus_client missing."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "prometheus_client" or name.startswith("prometheus_client."):
            raise ImportError("simulated: prometheus_client not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "prometheus_client", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    module = _reload_metrics()
    yield module
    monkeypatch.setattr(builtins, "__import__", real_import)
    _reload_metrics()


# ---- present --------------------------------------------------------------
def test_present_reexports_real_classes(metrics_present):
    assert metrics_present.HAS_PROMETHEUS is True
    from prometheus_client import Counter as RealCounter

    assert metrics_present.Counter is RealCounter


def test_present_counter_works(metrics_present):
    c = metrics_present.Counter("tc_test_total", "desc", ["kind"])
    c.labels(kind="a").inc()  # should not raise


def test_present_start_server(metrics_present, monkeypatch):
    called = {}
    monkeypatch.setattr(
        metrics_present, "start_http_server", lambda port: called.setdefault("port", port)
    )
    assert metrics_present.start_metrics_server(port=19191) is True
    assert called["port"] == 19191


def test_present_start_server_handles_failure(metrics_present, monkeypatch):
    def boom(port):
        raise OSError("address in use")

    monkeypatch.setattr(metrics_present, "start_http_server", boom)
    assert metrics_present.start_metrics_server(port=19192) is False


# ---- absent ---------------------------------------------------------------
def test_absent_uses_noops(metrics_absent):
    assert metrics_absent.HAS_PROMETHEUS is False


def test_absent_metric_methods_are_noops(metrics_absent):
    counter = metrics_absent.Counter("ignored", "desc", ["x"])
    gauge = metrics_absent.Gauge("ignored", "desc")
    hist = metrics_absent.Histogram("ignored", "desc")
    # All of these must be safe no-ops chaining through .labels()
    counter.labels(x="v").inc()
    counter.inc(5)
    gauge.set(3.14)
    gauge.dec()
    hist.observe(0.42)
    hist.labels(x="v").observe(1.0)


def test_absent_start_server_returns_false(metrics_absent):
    assert metrics_absent.start_metrics_server(port=9090) is False


def test_absent_start_http_server_is_noop(metrics_absent):
    # The shimmed start_http_server must accept a port and do nothing.
    assert metrics_absent.start_http_server(9090) is None


# ---- v0.2.0 additions -----------------------------------------------------
def test_noop_metric_class_is_exported():
    import trading_commons.metrics as m

    assert hasattr(m, "NoOpMetric")
    metric = m.NoOpMetric()
    # Full surface, all chainable / no-op.
    metric.inc()
    metric.inc(2)
    metric.dec()
    metric.dec(2)
    metric.set(1.0)
    metric.observe(0.5)
    metric.set_to_current_time()
    assert metric.labels(x="v") is metric
    metric.labels(x="v").inc()
    metric.labels(x="v").set_to_current_time()


def test_has_prometheus_private_alias_matches():
    import trading_commons.metrics as m

    assert m._HAS_PROMETHEUS == m.HAS_PROMETHEUS


def test_absent_noop_supports_set_to_current_time(metrics_absent):
    gauge = metrics_absent.Gauge("ignored", "desc")
    gauge.set_to_current_time()  # must not raise
    gauge.labels(x="v").set_to_current_time()
    assert metrics_absent._HAS_PROMETHEUS is False


def test_present_alias_true(metrics_present):
    assert metrics_present._HAS_PROMETHEUS is True
