"""Tests for trading_commons.telegram — sync + async send, retry, no-op."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from trading_commons.telegram import TelegramClient


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)

    async def _fake_async_sleep(_s):
        return None

    monkeypatch.setattr("trading_commons.telegram.asyncio.sleep", _fake_async_sleep)


def _resp(status: int):
    r = MagicMock()
    r.status_code = status
    r.text = "body"
    return r


# ---- no-op without creds --------------------------------------------------
def test_disabled_without_token():
    tg = TelegramClient(bot_token=None, chat_id="123")
    assert tg.enabled is False
    assert tg.send_message("hi") is False


def test_disabled_without_chat_id():
    tg = TelegramClient(bot_token="tok", chat_id=None)
    assert tg.enabled is False
    assert tg.send_message("hi") is False


def test_enabled_with_both():
    assert TelegramClient(bot_token="t", chat_id="c").enabled is True


# ---- sync send ------------------------------------------------------------
def test_sync_send_success(monkeypatch):
    tg = TelegramClient(bot_token="tok", chat_id="123")
    client_cm = MagicMock()
    client = MagicMock()
    client.post.return_value = _resp(200)
    client_cm.__enter__.return_value = client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client_cm)

    assert tg.send_message("<b>hi</b>") is True
    args, kwargs = client.post.call_args
    assert kwargs["json"]["parse_mode"] == "HTML"
    assert kwargs["json"]["chat_id"] == "123"
    assert kwargs["json"]["text"] == "<b>hi</b>"


def test_sync_send_retries_then_succeeds(monkeypatch):
    tg = TelegramClient(bot_token="tok", chat_id="123", max_retries=3)
    client = MagicMock()
    # First two attempts raise, third returns 200
    client.post.side_effect = [httpx.ConnectError("x"), httpx.ConnectError("y"), _resp(200)]
    cm = MagicMock()
    cm.__enter__.return_value = client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: cm)

    assert tg.send_message("hi") is True
    assert client.post.call_count == 3


def test_sync_send_all_attempts_fail(monkeypatch):
    tg = TelegramClient(bot_token="tok", chat_id="123", max_retries=3)
    client = MagicMock()
    client.post.return_value = _resp(500)
    cm = MagicMock()
    cm.__enter__.return_value = client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: cm)

    assert tg.send_message("hi") is False
    assert client.post.call_count == 3


def test_sync_send_non_200_then_no_more_retries(monkeypatch):
    tg = TelegramClient(bot_token="tok", chat_id="123", max_retries=1)
    client = MagicMock()
    client.post.return_value = _resp(403)
    cm = MagicMock()
    cm.__enter__.return_value = client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: cm)
    assert tg.send_message("hi") is False
    assert client.post.call_count == 1


# ---- async send -----------------------------------------------------------
async def test_async_disabled():
    tg = TelegramClient(bot_token=None, chat_id=None)
    assert await tg.send_message_async("hi") is False


async def test_async_send_success(monkeypatch):
    tg = TelegramClient(bot_token="tok", chat_id="123")

    client = MagicMock()

    async def _post(*a, **k):
        return _resp(200)

    client.post = _post
    cm = MagicMock()
    cm.__aenter__ = _make_aenter(client)
    cm.__aexit__ = _make_aexit()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: cm)

    assert await tg.send_message_async("hi", parse_mode="HTML") is True


async def test_async_send_retries_then_succeeds(monkeypatch):
    tg = TelegramClient(bot_token="tok", chat_id="123", max_retries=3)
    calls = {"n": 0}

    async def _post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return _resp(200)

    client = MagicMock()
    client.post = _post
    cm = MagicMock()
    cm.__aenter__ = _make_aenter(client)
    cm.__aexit__ = _make_aexit()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: cm)

    assert await tg.send_message_async("hi") is True
    assert calls["n"] == 3


async def test_async_send_all_fail(monkeypatch):
    tg = TelegramClient(bot_token="tok", chat_id="123", max_retries=2)

    async def _post(*a, **k):
        raise httpx.ConnectError("down")

    client = MagicMock()
    client.post = _post
    cm = MagicMock()
    cm.__aenter__ = _make_aenter(client)
    cm.__aexit__ = _make_aexit()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: cm)

    assert await tg.send_message_async("hi") is False


def _make_aenter(value):
    async def _aenter(_self):
        return value

    return _aenter


def _make_aexit():
    async def _aexit(_self, *exc):
        return False

    return _aexit


def test_backoff_grows():
    tg = TelegramClient(bot_token="t", chat_id="c", backoff_base=1.0)
    assert tg._backoff(1) == 1.0
    assert tg._backoff(2) == 2.0
    assert tg._backoff(3) == 4.0
