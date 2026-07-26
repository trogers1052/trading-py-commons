"""Tests for trading_commons.clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_commons.clock import (
    DEFAULT_SIM_KEY,
    MODE_REAL,
    MODE_REPLAY,
    ClockError,
    ManualClock,
    RedisSource,
    ReplayClock,
    SystemClock,
    from_env,
    mode_from_env,
)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class FakeSource:
    """A Source whose result the test controls."""

    def __init__(self, value: datetime | None = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.reads = 0

    def read(self) -> datetime:
        self.reads += 1
        if self.error is not None:
            raise self.error
        assert self.value is not None
        return self.value


class FakeRedis:
    """Minimal Redis stand-in exposing only get()."""

    def __init__(self, value: object = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.key: str | None = None

    def get(self, key: str) -> object:
        self.key = key
        if self.error is not None:
            raise self.error
        return self.value


# --- SystemClock -------------------------------------------------------------


def test_system_clock_returns_aware_utc():
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert abs((datetime.now(UTC) - now).total_seconds()) < 60


# --- ManualClock -------------------------------------------------------------


def test_manual_clock_set_and_advance():
    clock = ManualClock(utc("2021-03-01T14:30:00Z"))
    assert clock.now() == utc("2021-03-01T14:30:00Z")

    clock.advance(hours=2)
    assert clock.now() == utc("2021-03-01T16:30:00Z")

    clock.advance(minutes=-30)
    assert clock.now() == utc("2021-03-01T16:00:00Z")

    clock.set(utc("2022-01-01T00:00:00Z"))
    assert clock.now() == utc("2022-01-01T00:00:00Z")

    clock.advance(days=1, seconds=30)
    assert clock.now() == utc("2022-01-02T00:00:30Z")


def test_manual_clock_treats_naive_input_as_utc():
    clock = ManualClock(datetime(2021, 3, 1, 14, 30))

    assert clock.now() == utc("2021-03-01T14:30:00Z")


# --- ReplayClock -------------------------------------------------------------


def test_replay_clock_prime_then_now():
    src = FakeSource(utc("2021-03-01T14:30:00Z"))
    clock = ReplayClock(src)
    clock.prime()

    assert clock.now() == utc("2021-03-01T14:30:00Z")

    src.value = utc("2021-03-02T14:30:00Z")
    assert clock.now() == utc("2021-03-02T14:30:00Z")


def test_replay_clock_now_before_prime_raises():
    clock = ReplayClock(FakeSource(utc("2021-03-01T00:00:00Z")))

    with pytest.raises(ClockError, match="before prime"):
        clock.now()


def test_replay_clock_prime_propagates_read_failure():
    clock = ReplayClock(FakeSource(error=ClockError("redis down")))

    with pytest.raises(ClockError, match="redis down"):
        clock.prime()


def test_replay_clock_never_falls_back_to_wall_clock():
    """The core safety property of the whole replay design."""
    simulated = utc("2021-03-01T14:30:00Z")
    src = FakeSource(simulated)
    clock = ReplayClock(src)
    clock.prime()

    src.error = ClockError("redis down")
    held = clock.now()

    assert held == simulated, "must hold last-known-good during an outage"
    assert clock.degraded is True
    # If it had fallen back to the wall clock this would be ~now, not 2021.
    assert (datetime.now(UTC) - held) > timedelta(days=365)

    src.error = None
    src.value = utc("2021-03-03T14:30:00Z")
    assert clock.now() == utc("2021-03-03T14:30:00Z")
    assert clock.degraded is False


def test_replay_clock_serves_backwards_time():
    src = FakeSource(utc("2021-03-05T00:00:00Z"))
    clock = ReplayClock(src)
    clock.prime()

    src.value = utc("2021-03-01T00:00:00Z")

    # The driver is authoritative even when it rewinds (e.g. a new run).
    assert clock.now() == utc("2021-03-01T00:00:00Z")
    assert clock.now() == utc("2021-03-01T00:00:00Z")


def test_replay_clock_min_refresh_throttles_reads():
    src = FakeSource(utc("2021-03-01T00:00:00Z"))
    clock = ReplayClock(src, min_refresh_seconds=3600)
    clock.prime()
    after_prime = src.reads

    for _ in range(5):
        clock.now()

    assert src.reads == after_prime, "min_refresh should serve the cached instant"


# --- RedisSource -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2021-03-01T14:30:00Z", "2021-03-01T14:30:00Z"),
        ("2021-03-01T14:30:00+00:00", "2021-03-01T14:30:00Z"),
        ("2021-03-01T09:30:00-05:00", "2021-03-01T14:30:00Z"),
        (b"2021-03-01T14:30:00Z", "2021-03-01T14:30:00Z"),
        ("2021-03-01T14:30:00", "2021-03-01T14:30:00Z"),  # naive treated as UTC
    ],
)
def test_redis_source_parses(raw, expected):
    src = RedisSource(FakeRedis(raw))

    assert src.read() == utc(expected)


def test_redis_source_missing_key():
    src = RedisSource(FakeRedis(None))

    with pytest.raises(ClockError, match="is not set"):
        src.read()


def test_redis_source_unparseable_value():
    src = RedisSource(FakeRedis("1614609000"))

    with pytest.raises(ClockError, match="ISO-8601"):
        src.read()


def test_redis_source_client_failure():
    src = RedisSource(FakeRedis(error=ConnectionError("refused")))

    with pytest.raises(ClockError, match="reading simulated clock"):
        src.read()


def test_redis_source_defaults_key():
    fake = FakeRedis("2021-03-01T14:30:00Z")
    RedisSource(fake).read()

    assert fake.key == DEFAULT_SIM_KEY


# --- Wiring ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ({}, MODE_REAL),
        ({"CLOCK_MODE": "replay"}, MODE_REPLAY),
        ({"CLOCK_MODE": "raplay"}, MODE_REAL),  # typo must not enable replay
        ({"CLOCK_MODE": "REPLAY"}, MODE_REAL),  # case sensitive
        ({"CLOCK_MODE": "real"}, MODE_REAL),
    ],
)
def test_mode_from_env(value, expected):
    assert mode_from_env(value) == expected


def test_from_env_defaults_to_system_clock():
    assert isinstance(from_env(environ={}), SystemClock)


def test_from_env_replay_requires_client():
    with pytest.raises(ClockError, match="requires a Redis client"):
        from_env(environ={"CLOCK_MODE": "replay"})


def test_from_env_replay_primes_from_redis():
    fake = FakeRedis("2021-03-01T14:30:00Z")

    clock = from_env(fake, environ={"CLOCK_MODE": "replay", "CLOCK_SIM_KEY": "sim:clock:test"})

    assert isinstance(clock, ReplayClock)
    assert fake.key == "sim:clock:test"
    assert clock.now() == utc("2021-03-01T14:30:00Z")


def test_from_env_replay_fails_when_key_missing():
    with pytest.raises(ClockError, match="is not set"):
        from_env(FakeRedis(None), environ={"CLOCK_MODE": "replay"})


def test_from_env_reads_os_environ_by_default(monkeypatch):
    monkeypatch.delenv("CLOCK_MODE", raising=False)
    assert isinstance(from_env(), SystemClock)

    monkeypatch.setenv("CLOCK_MODE", "replay")
    with pytest.raises(ClockError):
        from_env()
