"""Telegram client with sync + async send, retry, and graceful no-op.

Generalised from ``stop-loss-guardian``'s httpx-based telegram client (the
most complete in the platform). Features:

- Both :meth:`TelegramClient.send_message` (sync) and
  :meth:`TelegramClient.send_message_async` (async), HTML parse mode default.
- Exponential-backoff retry on transient failures.
- Graceful no-op when no ``bot_token``/``chat_id`` is configured — returns
  ``False`` and logs, never raises. Services can wire it up unconditionally.

Usage::

    from trading_commons.telegram import TelegramClient

    tg = TelegramClient(bot_token=tok, chat_id=cid)   # or from settings
    tg.send_message("<b>Stop hit</b> on AAPL")        # sync

    await tg.send_message_async("queued setup ready")  # async
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)


class TelegramClient:
    """Sends Telegram messages with retry and a no-op-without-creds fallback.

    Parameters
    ----------
    bot_token, chat_id:
        Telegram credentials. If either is missing the client is *disabled*
        and every send is a logged no-op returning ``False``.
    max_retries:
        Total attempts per send (default 3).
    timeout:
        Per-request timeout in seconds (default 10).
    backoff_base:
        Base seconds for exponential backoff; attempt *n* waits
        ``backoff_base * 2**(n-1)`` (default 1.0 → 1s, 2s, 4s...).
    """

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | int | None,
        *,
        max_retries: int = 3,
        timeout: float = 10.0,
        backoff_base: float = 1.0,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.max_retries = max_retries
        self.timeout = timeout
        self.backoff_base = backoff_base
        self.base_url = f"https://api.telegram.org/bot{bot_token}" if bot_token else ""

    @property
    def enabled(self) -> bool:
        """True only when both a bot token and chat id are configured."""
        return bool(self.bot_token and self.chat_id)

    def _payload(self, message: str, parse_mode: str) -> dict[str, object]:
        return {"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode}

    def _backoff(self, attempt: int) -> float:
        """Seconds to wait before the next attempt (1-indexed)."""
        return self.backoff_base * (2 ** (attempt - 1))

    # ---- sync -----------------------------------------------------------
    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message synchronously with exponential-backoff retry.

        Returns ``True`` on success, ``False`` if disabled or all attempts
        failed. Never raises.
        """
        if not self.enabled:
            logger.warning("Telegram not configured — skipping message")
            return False

        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client() as client:
                    response = client.post(
                        f"{self.base_url}/sendMessage",
                        json=self._payload(message, parse_mode),
                        timeout=self.timeout,
                    )
                if response.status_code == 200:
                    logger.debug("Telegram message sent")
                    return True
                last_error = f"HTTP {response.status_code}: {response.text}"
                logger.error("Telegram API error: %s", last_error)
            except Exception as exc:  # noqa: BLE001 - retry any transport error
                last_error = str(exc)
                logger.error(
                    "Telegram send failed (attempt %d/%d): %s",
                    attempt,
                    self.max_retries,
                    exc,
                )
            if attempt < self.max_retries:
                time.sleep(self._backoff(attempt))

        logger.error("All %d Telegram send attempts failed: %s", self.max_retries, last_error)
        return False

    # ---- async ----------------------------------------------------------
    async def send_message_async(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message asynchronously with exponential-backoff retry.

        Returns ``True`` on success, ``False`` if disabled or all attempts
        failed. Never raises.
        """
        if not self.enabled:
            logger.warning("Telegram not configured — skipping message")
            return False

        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self.base_url}/sendMessage",
                        json=self._payload(message, parse_mode),
                        timeout=self.timeout,
                    )
                if response.status_code == 200:
                    logger.debug("Telegram message sent")
                    return True
                last_error = f"HTTP {response.status_code}: {response.text}"
                logger.error("Telegram API error: %s", last_error)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.error(
                    "Telegram send failed (attempt %d/%d): %s",
                    attempt,
                    self.max_retries,
                    exc,
                )
            if attempt < self.max_retries:
                await asyncio.sleep(self._backoff(attempt))

        logger.error("All %d Telegram send attempts failed: %s", self.max_retries, last_error)
        return False


__all__ = ["TelegramClient"]
