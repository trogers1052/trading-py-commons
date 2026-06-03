"""Tests for trading_commons.redisx — connect, reconnect, retry (mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import redis

from trading_commons.redisx import RedisBase, redis_url


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Make backoff sleeps instant.
    monkeypatch.setattr("trading_commons.redisx.time.sleep", lambda _s: None)


def test_redis_url_helper():
    assert redis_url("h", 6379, 0) == "redis://h:6379/0"
    assert redis_url("h", 6380, 1, "pw") == "redis://:pw@h:6380/1"


def test_redis_url_property():
    base = RedisBase(host="h", port=6380, db=2, password="pw")
    assert base.redis_url == "redis://:pw@h:6380/2"


def test_client_raises_before_connect():
    base = RedisBase()
    with pytest.raises(RuntimeError):
        _ = base.client


def test_connect_success(monkeypatch):
    fake = MagicMock()
    fake.ping.return_value = True
    base = RedisBase()
    monkeypatch.setattr(base, "_create_client", lambda: fake)
    assert base.connect() is True
    assert base.client is fake
    fake.ping.assert_called_once()


def test_connect_failure(monkeypatch):
    def boom():
        client = MagicMock()
        client.ping.side_effect = redis.RedisError("nope")
        return client

    base = RedisBase()
    monkeypatch.setattr(base, "_create_client", boom)
    assert base.connect() is False
    with pytest.raises(RuntimeError):
        _ = base.client


def test_with_retry_success_no_reconnect(monkeypatch):
    base = RedisBase()
    fake = MagicMock()
    monkeypatch.setattr(base, "_create_client", lambda: fake)
    base.connect()
    func = MagicMock(return_value="ok")
    assert base._with_retry(func, "arg", kw=1) == "ok"
    func.assert_called_once_with("arg", kw=1)


def test_with_retry_reconnects_then_succeeds(monkeypatch):
    base = RedisBase(max_retries=3)
    fake = MagicMock()
    monkeypatch.setattr(base, "_create_client", lambda: fake)
    base.connect()

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise redis.ConnectionError("dropped")
        return "recovered"

    reconnect_spy = MagicMock(return_value=True)
    monkeypatch.setattr(base, "reconnect", reconnect_spy)

    assert base._with_retry(flaky) == "recovered"
    assert calls["n"] == 2
    reconnect_spy.assert_called_once()


def test_with_retry_exhausts_and_raises(monkeypatch):
    base = RedisBase(max_retries=2)
    fake = MagicMock()
    monkeypatch.setattr(base, "_create_client", lambda: fake)
    base.connect()

    def always_fail():
        raise redis.TimeoutError("timeout")

    monkeypatch.setattr(base, "reconnect", MagicMock(return_value=True))

    with pytest.raises(redis.TimeoutError):
        base._with_retry(always_fail)
    # 1 initial + 2 retries == reconnect called twice
    assert base.reconnect.call_count == 2


def test_with_retry_gives_up_if_reconnect_fails(monkeypatch):
    base = RedisBase(max_retries=3)
    fake = MagicMock()
    monkeypatch.setattr(base, "_create_client", lambda: fake)
    base.connect()

    def always_fail():
        raise redis.ConnectionError("dead")

    # reconnect never succeeds → still loops through max_retries then raises
    monkeypatch.setattr(base, "reconnect", MagicMock(return_value=False))
    with pytest.raises(redis.ConnectionError):
        base._with_retry(always_fail)
    assert base.reconnect.call_count == 3


def test_reconnect_success(monkeypatch):
    base = RedisBase()
    old = MagicMock()
    base._client = old
    new = MagicMock()
    new.ping.return_value = True
    monkeypatch.setattr(base, "_create_client", lambda: new)
    assert base.reconnect() is True
    old.close.assert_called_once()
    assert base.client is new


def test_reconnect_failure(monkeypatch):
    base = RedisBase()

    def boom():
        c = MagicMock()
        c.ping.side_effect = redis.RedisError("still down")
        return c

    monkeypatch.setattr(base, "_create_client", boom)
    assert base.reconnect() is False
    assert base._client is None


def test_close_is_idempotent(monkeypatch):
    base = RedisBase()
    fake = MagicMock()
    monkeypatch.setattr(base, "_create_client", lambda: fake)
    base.connect()
    base.close()
    fake.close.assert_called_once()
    assert base._client is None
    base.close()  # second close must not raise
