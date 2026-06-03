"""Redis connection base with reconnect + retry.

:class:`RedisBase` provides the connection lifecycle shared by every Redis
store across the platform: ``connect`` / ``reconnect`` / ``_with_retry`` (with
exponential backoff) and sensible socket timeouts. Generalised from
``robinhood-sync``'s ``_RedisBase``.

Subclass it and use ``self._with_retry(...)`` for every Redis call::

    class SyncedOrders(RedisBase):
        def is_synced(self, oid: str) -> bool:
            return self._with_retry(self.client.sismember, "synced", oid)

    store = SyncedOrders(host="localhost", port=6379, db=0)
    store.connect()
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import redis

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Exceptions that indicate a transient connection problem worth retrying.
_RETRYABLE = (
    redis.ConnectionError,
    redis.TimeoutError,
    ConnectionError,
    TimeoutError,
)


def redis_url(
    host: str,
    port: int = 6379,
    db: int = 0,
    password: str | None = None,
) -> str:
    """Assemble a Redis connection URL (includes the password if set)."""
    if password:
        return f"redis://:{password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"


class RedisBase:
    """Base class providing Redis connection, reconnection, and retry logic.

    Parameters
    ----------
    host, port, db, password:
        Standard Redis connection parameters.
    socket_timeout, socket_connect_timeout:
        Socket timeouts in seconds (default 5).
    max_retries:
        Number of reconnect attempts ``_with_retry`` makes on a transient
        failure before giving up (default 3). ``0`` means *call once, no
        retry, no sleep*.
    backoff_base:
        Base seconds for exponential backoff between reconnect attempts;
        attempt *n* waits ``backoff_base * 2**(n-1)`` (default 0.5).
    decode_responses:
        Passed through to ``redis.Redis`` (default True).
    retry_on_timeout:
        Passed through to ``redis.Redis`` (default False). Set ``True`` to
        let redis-py retry on socket timeouts.
    client_factory:
        Optional callable used to build the underlying client instead of
        ``redis.Redis(...)``. A clean injection/test seam — provide it to
        supply a custom or mock client without overriding ``_create_client``.
        It is called with the same keyword arguments ``_create_client``
        would pass to ``redis.Redis``.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        *,
        socket_timeout: float = 5.0,
        socket_connect_timeout: float = 5.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        decode_responses: bool = True,
        retry_on_timeout: bool = False,
        client_factory: Callable[..., redis.Redis] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.socket_timeout = socket_timeout
        self.socket_connect_timeout = socket_connect_timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.decode_responses = decode_responses
        self.retry_on_timeout = retry_on_timeout
        self.client_factory = client_factory
        self._client: redis.Redis | None = None

    @property
    def redis_url(self) -> str:
        """Return this connection's Redis URL."""
        return redis_url(self.host, self.port, self.db, self.password)

    @property
    def client(self) -> redis.Redis | None:
        """Return the underlying client, or ``None`` when disconnected.

        Returns ``self._client`` directly (which is ``None`` before
        :meth:`connect` or after :meth:`close`). Callers may treat a
        disconnected store as falsy rather than guarding against an
        exception.
        """
        return self._client

    def _create_client(self) -> redis.Redis:
        """Create a new configured Redis client instance.

        Uses ``client_factory`` when one was supplied, else ``redis.Redis``.
        An empty-string password is normalised to ``None``.
        """
        kwargs: dict[str, Any] = dict(
            host=self.host,
            port=self.port,
            db=self.db,
            password=self.password or None,
            decode_responses=self.decode_responses,
            socket_timeout=self.socket_timeout,
            socket_connect_timeout=self.socket_connect_timeout,
            retry_on_timeout=self.retry_on_timeout,
        )
        if self.client_factory is not None:
            return self.client_factory(**kwargs)
        return redis.Redis(**kwargs)

    def connect(self) -> bool:
        """Create the client and verify the connection with PING.

        Returns ``True`` on success, ``False`` on failure (logged, not raised).
        """
        try:
            logger.info(
                "%s connecting to Redis at %s:%d/%d",
                self.__class__.__name__,
                self.host,
                self.port,
                self.db,
            )
            self._client = self._create_client()
            self._client.ping()
            logger.info("%s connected to Redis", self.__class__.__name__)
            return True
        except redis.RedisError as exc:
            logger.error("%s failed to connect to Redis: %s", self.__class__.__name__, exc)
            self._client = None
            return False

    def reconnect(self) -> bool:
        """Tear down and recreate the client, verifying with PING.

        Returns ``True`` if reconnection succeeded.
        """
        logger.warning("%s attempting Redis reconnection...", self.__class__.__name__)
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - best-effort close
                pass
        try:
            self._client = self._create_client()
            self._client.ping()
            logger.info("%s reconnected to Redis", self.__class__.__name__)
            return True
        except redis.RedisError as exc:
            logger.error("%s reconnection failed: %s", self.__class__.__name__, exc)
            self._client = None
            return False

    def _with_retry(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Execute a Redis operation, reconnecting with backoff on failure.

        On a transient connection error, reconnects (up to ``max_retries``
        times with exponential backoff) and retries the operation. Re-raises
        the last error if all attempts are exhausted.

        When ``max_retries == 0`` the operation is called exactly once: any
        transient error propagates immediately with no reconnect and no
        sleep.
        """
        try:
            return func(*args, **kwargs)
        except _RETRYABLE as exc:
            if self.max_retries <= 0:
                raise
            last_exc: BaseException = exc
            logger.warning(
                "%s Redis op failed: %s — will reconnect and retry",
                self.__class__.__name__,
                exc,
            )
            for attempt in range(1, self.max_retries + 1):
                delay = self.backoff_base * (2 ** (attempt - 1))
                time.sleep(delay)
                if self.reconnect():
                    try:
                        return func(*args, **kwargs)
                    except _RETRYABLE as retry_exc:
                        last_exc = retry_exc
                        logger.warning(
                            "%s retry %d/%d failed: %s",
                            self.__class__.__name__,
                            attempt,
                            self.max_retries,
                            retry_exc,
                        )
            raise last_exc from None

    def close(self) -> None:
        """Close the Redis connection (best-effort)."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            logger.info("%s Redis connection closed", self.__class__.__name__)


__all__ = ["RedisBase", "redis_url"]
