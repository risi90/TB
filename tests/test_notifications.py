"""Tests for the webhook notification dispatcher (no network involved)."""

from __future__ import annotations

from typing import Any

from utils.notifications import Notifier


def _capture(notifier: Notifier) -> list[tuple[str, dict[str, Any]]]:
    """Replace the network layer with a recorder; returns the call log."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_post(url: str, payload: dict[str, Any]) -> bool:
        calls.append((url, payload))
        return True

    notifier._post = fake_post  # type: ignore[method-assign]
    return calls


def test_disabled_without_any_channel() -> None:
    assert not Notifier().enabled
    assert not Notifier(telegram_bot_token="token-only").enabled  # chat id missing
    assert Notifier(discord_webhook_url="https://discord/hook").enabled
    assert Notifier(telegram_bot_token="t", telegram_chat_id="42").enabled


async def test_disabled_send_is_noop() -> None:
    notifier = Notifier()
    calls = _capture(notifier)
    assert await notifier.send("hello") is False
    assert calls == []


async def test_send_targets_all_configured_channels() -> None:
    notifier = Notifier(
        telegram_bot_token="TOKEN", telegram_chat_id="42",
        discord_webhook_url="https://discord.example/hook",
    )
    calls = _capture(notifier)
    assert await notifier.send("hello") is True
    assert len(calls) == 2
    telegram = next(c for c in calls if "telegram" in c[0])
    discord = next(c for c in calls if "discord" in c[0])
    assert "botTOKEN/sendMessage" in telegram[0]
    assert telegram[1] == {"chat_id": "42", "text": "hello"}
    assert discord[1] == {"content": "hello"}
    assert set(notifier.channels) == {"telegram", "discord"}


async def test_send_throttled_suppresses_repeats_per_key() -> None:
    notifier = Notifier(discord_webhook_url="https://discord.example/hook")
    calls = _capture(notifier)
    assert await notifier.send_throttled("ws", "disconnect 1", min_interval=300) is True
    assert await notifier.send_throttled("ws", "disconnect 2", min_interval=300) is False
    assert await notifier.send_throttled("other", "different key", min_interval=300) is True
    assert len(calls) == 2
