"""Lightweight async notification dispatcher for Telegram and Discord.

Channels are configured via ``.env`` (``TELEGRAM_BOT_TOKEN`` +
``TELEGRAM_CHAT_ID`` and/or ``DISCORD_WEBHOOK_URL``); with no channel
configured the notifier is disabled and every ``send`` is a cheap no-op, so
call sites never need to guard. Delivery is best-effort: failures are logged
and never raised into the trading loop. ``send_throttled`` rate-limits noisy
event classes (e.g. WebSocket reconnect storms) per key.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from loguru import logger

if TYPE_CHECKING:
    from config.config import Settings

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class Notifier:
    """Dispatches alert messages to all configured webhook channels.

    Args:
        telegram_bot_token: Telegram Bot API token (from @BotFather).
        telegram_chat_id: Target chat id for Telegram messages.
        discord_webhook_url: Discord channel webhook URL.
    """

    def __init__(
        self,
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        discord_webhook_url: str = "",
    ) -> None:
        self._telegram_token = telegram_bot_token.strip()
        self._telegram_chat_id = telegram_chat_id.strip()
        self._discord_webhook_url = discord_webhook_url.strip()
        self._session: aiohttp.ClientSession | None = None
        self._last_sent: dict[str, float] = {}

    @classmethod
    def from_settings(cls, settings: "Settings") -> "Notifier":
        """Build a notifier from application settings."""
        return cls(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            discord_webhook_url=settings.discord_webhook_url,
        )

    @property
    def enabled(self) -> bool:
        """Whether at least one channel is fully configured."""
        return bool(
            (self._telegram_token and self._telegram_chat_id)
            or self._discord_webhook_url
        )

    @property
    def channels(self) -> list[str]:
        """Names of the configured channels (for status displays)."""
        result = []
        if self._telegram_token and self._telegram_chat_id:
            result.append("telegram")
        if self._discord_webhook_url:
            result.append("discord")
        return result

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    async def send(self, message: str) -> bool:
        """Send ``message`` to every configured channel.

        Returns ``True`` if at least one channel accepted the message;
        ``False`` when disabled or all deliveries failed. Never raises.
        """
        if not self.enabled:
            return False
        tasks = []
        if self._telegram_token and self._telegram_chat_id:
            tasks.append(
                self._post(
                    f"https://api.telegram.org/bot{self._telegram_token}/sendMessage",
                    {"chat_id": self._telegram_chat_id, "text": message},
                )
            )
        if self._discord_webhook_url:
            tasks.append(self._post(self._discord_webhook_url, {"content": message}))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return any(r is True for r in results)

    async def send_throttled(
        self, key: str, message: str, min_interval: float = 300.0
    ) -> bool:
        """Send at most one message per ``key`` per ``min_interval`` seconds.

        Use for event classes that can storm (reconnect loops). Suppressed
        messages return ``False`` without touching the network.
        """
        now = time.monotonic()
        if now - self._last_sent.get(key, float("-inf")) < min_interval:
            return False
        self._last_sent[key] = now
        return await self.send(message)

    async def close(self) -> None:
        """Release the HTTP session (idempotent)."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _post(self, url: str, payload: dict[str, Any]) -> bool:
        try:
            if self._session is None or self._session.closed:
                # trust_env honors standard HTTP(S)_PROXY / CA env vars.
                self._session = aiohttp.ClientSession(trust_env=True)
            async with self._session.post(
                url, json=payload, timeout=_REQUEST_TIMEOUT
            ) as response:
                if response.status >= 300:
                    body = (await response.text())[:200]
                    logger.warning(
                        "Notification endpoint returned {}: {}", response.status, body
                    )
                    return False
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Notification delivery failed: {}", exc)
            return False
