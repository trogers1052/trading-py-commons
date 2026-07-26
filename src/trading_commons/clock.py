"""The time source a service reads "now" from.

Services normally call ``datetime.now()`` directly, which makes them impossible
to replay: every relative-duration gate in the platform (signal debounce,
context staleness, earnings staleness, cache TTLs) compares *now* against a
stored instant, so driving historical data past a wall clock makes those gates
behave in ways that never happen in production. Reading time through a
:class:`Clock` lets the e2e-replay harness drive simulated time instead::

    from trading_commons.clock import Clock, from_env

    clock = from_env(redis_client)          # SystemClock unless CLOCK_MODE=replay
    if (clock.now() - last_seen).total_seconds() < debounce:
        ...

The default is always the real system clock. Replay is opt-in via
``CLOCK_MODE=replay``, so a service that adopts this module behaves exactly as
it did before unless it is deliberately put into a replay.

Failing loud
------------
:class:`ReplayClock` **never** falls back to wall-clock time. A silent fallback
would stamp today's date onto a replay of 2021 and corrupt the run's results
invisibly — the worst possible failure, because the output still looks
plausible. Instead the clock must be primed before use (:meth:`ReplayClock.prime`
raises if simulated time is unreadable) and afterwards serves the
last-known-good instant, logging loudly, until the source recovers.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Redis key the replay harness publishes simulated time to, as an ISO-8601
#: UTC timestamp. A string rather than an epoch number so that
#: ``redis-cli GET sim:clock`` is readable during a run.
DEFAULT_SIM_KEY = "sim:clock"

MODE_REAL = "real"
MODE_REPLAY = "replay"


@runtime_checkable
class Clock(Protocol):
    """Reports the current time. Implementations must be thread-safe."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...


class SystemClock:
    """Reads the real wall clock. The default everywhere.

    Returns UTC rather than local time because every timestamp the platform
    persists or publishes is UTC, and a local-time clock would silently shift
    them.
    """

    def now(self) -> datetime:
        """Return the current wall-clock time as an aware UTC datetime."""
        return datetime.now(UTC)


class ManualClock:
    """A clock whose time is set explicitly, for unit tests.

    A test that needs "two hours have passed" calls :meth:`advance` rather than
    sleeping::

        clock = ManualClock(datetime(2021, 3, 1, tzinfo=UTC))
        clock.advance(hours=2)
    """

    def __init__(self, start: datetime) -> None:
        self._lock = threading.RLock()
        self._now = _as_utc(start)

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def set(self, value: datetime) -> None:
        """Move the clock to ``value``."""
        with self._lock:
            self._now = _as_utc(value)

    def advance(
        self, seconds: float = 0, *, minutes: float = 0, hours: float = 0, days: float = 0
    ) -> None:
        """Move the clock forward. Negative values are allowed."""
        from datetime import timedelta

        delta = timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        with self._lock:
            self._now = self._now + delta


class ClockError(RuntimeError):
    """Raised when simulated time cannot be established."""


@runtime_checkable
class Source(Protocol):
    """Reads the current simulated instant.

    The seam that keeps :class:`ReplayClock` testable without a Redis server.
    """

    def read(self) -> datetime:
        """Return the simulated instant, or raise :class:`ClockError`."""
        ...


class RedisSource:
    """Reads simulated time from a Redis key written by the replay driver."""

    def __init__(self, client: Any, key: str = DEFAULT_SIM_KEY) -> None:
        self._client = client
        self._key = key

    @property
    def key(self) -> str:
        return self._key

    def read(self) -> datetime:
        try:
            raw = self._client.get(self._key)
        except Exception as exc:  # noqa: BLE001 - any client failure is a read failure
            raise ClockError(f"reading simulated clock {self._key!r}: {exc}") from exc

        if raw is None:
            raise ClockError(
                f"simulated clock key {self._key!r} is not set: is the replay driver running?"
            )
        if isinstance(raw, bytes):
            raw = raw.decode()

        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ClockError(
                f"simulated clock {self._key!r} holds {raw!r}, want an ISO-8601 timestamp"
            ) from exc
        return _as_utc(parsed)


class ReplayClock:
    """Reports simulated time supplied by the replay harness.

    Must be primed with :meth:`prime` before first use. It never returns
    wall-clock time: if the source fails after priming it keeps serving the
    last-known-good instant and logs, so a broken replay stalls visibly instead
    of silently jumping to the present.
    """

    def __init__(self, source: Source, min_refresh_seconds: float = 0.0) -> None:
        self._source = source
        self._min_refresh = min_refresh_seconds
        self._lock = threading.RLock()
        self._last: datetime | None = None
        self._last_read_monotonic: float | None = None
        self._degraded = False
        self._warned_for: datetime | None = None

    def prime(self) -> None:
        """Perform the first read.

        Raises :class:`ClockError` if simulated time cannot be read, so a
        misconfigured replay fails at startup rather than producing a full run
        of quietly wrong results.
        """
        import time as _time

        value = self._source.read()
        with self._lock:
            self._last = value
            self._last_read_monotonic = _time.monotonic()

    @property
    def degraded(self) -> bool:
        """Whether the last read of the source failed."""
        with self._lock:
            return self._degraded

    def now(self) -> datetime:
        """Return the current simulated time.

        Calling this before :meth:`prime` raises, rather than guessing: a
        wall-clock guess would be indistinguishable from a correct answer in
        the output.
        """
        import time as _time

        with self._lock:
            last = self._last
            last_read = self._last_read_monotonic
            min_refresh = self._min_refresh

        if last is None:
            raise ClockError("replay clock used before prime()")

        if (
            min_refresh > 0
            and last_read is not None
            and (_time.monotonic() - last_read) < min_refresh
        ):
            return last

        try:
            value = self._source.read()
        except ClockError as exc:
            with self._lock:
                if not self._degraded:
                    self._degraded = True
                    logger.error(
                        "Simulated time unreadable (%s) — holding at %s; "
                        "NOT falling back to wall clock",
                        exc,
                        self._last.isoformat() if self._last else "<unset>",
                    )
                return self._last  # type: ignore[return-value]

        with self._lock:
            if self._degraded:
                self._degraded = False
                logger.info("Simulated time readable again at %s", value.isoformat())
            # Simulated time running backwards means a driver bug or a run
            # restart. Serve it — the driver is authoritative — but say so once.
            if self._last is not None and value < self._last and value != self._warned_for:
                logger.warning(
                    "Simulated time went backwards: %s -> %s",
                    self._last.isoformat(),
                    value.isoformat(),
                )
                self._warned_for = value
            self._last = value
            self._last_read_monotonic = _time.monotonic()
            return value


def mode_from_env(environ: Mapping[str, str] | None = None) -> str:
    """Return the configured clock mode, defaulting to ``"real"``.

    Any value other than ``"replay"`` is treated as real, so a typo can never
    silently put a production service onto a simulated clock.
    """
    env = os.environ if environ is None else environ
    return MODE_REPLAY if env.get("CLOCK_MODE", MODE_REAL) == MODE_REPLAY else MODE_REAL


def from_env(redis_client: Any = None, environ: Mapping[str, str] | None = None) -> Clock:
    """Return the :class:`Clock` a service should use.

    Returns a :class:`SystemClock` unless ``CLOCK_MODE=replay``, in which case
    it builds a :class:`ReplayClock` reading ``CLOCK_SIM_KEY`` (default
    ``sim:clock``) via ``redis_client`` and primes it. A missing client in
    replay mode raises rather than silently downgrading to wall time.
    """
    env = os.environ if environ is None else environ
    if mode_from_env(env) == MODE_REAL:
        return SystemClock()
    if redis_client is None:
        raise ClockError("CLOCK_MODE=replay requires a Redis client")

    clock = ReplayClock(RedisSource(redis_client, env.get("CLOCK_SIM_KEY", DEFAULT_SIM_KEY)))
    clock.prime()
    return clock


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    Naive datetimes are assumed UTC — the platform persists and publishes UTC
    everywhere, so interpreting them as local time would shift every timestamp.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
