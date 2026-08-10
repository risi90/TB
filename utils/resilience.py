"""Resilience helpers for WebSocket / network error handling."""

from __future__ import annotations

import asyncio
import random


class ExponentialBackoff:
    """Exponential backoff with jitter for reconnect loops.

    Delays grow as ``base * factor**n`` up to ``max_delay``, with up to 25%
    random jitter added to avoid thundering-herd reconnects. Call
    :meth:`reset` after a successful operation so the next failure starts
    from the base delay again.
    """

    def __init__(
        self,
        base: float = 1.0,
        factor: float = 2.0,
        max_delay: float = 60.0,
    ) -> None:
        self.base = base
        self.factor = factor
        self.max_delay = max_delay
        self._attempt = 0

    @property
    def attempt(self) -> int:
        """Number of consecutive failures so far."""
        return self._attempt

    def next_delay(self) -> float:
        """Return the next delay in seconds and advance the attempt counter."""
        delay = min(self.base * (self.factor**self._attempt), self.max_delay)
        self._attempt += 1
        return delay * (1.0 + random.uniform(0.0, 0.25))

    async def sleep(self) -> float:
        """Sleep for the next backoff delay; returns the delay used."""
        delay = self.next_delay()
        await asyncio.sleep(delay)
        return delay

    def reset(self) -> None:
        """Reset the attempt counter after a successful operation."""
        self._attempt = 0
