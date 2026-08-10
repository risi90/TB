"""Graceful shutdown management for the trading bot.

:class:`ShutdownManager` centralises the shutdown story:

1. ``SIGINT`` / ``SIGTERM`` (or a dashboard "stop") trip a single event.
2. Registered async callbacks run **in registration order** — the app
   registers them so state is flushed to SQLite *before* network and
   database connections close.
3. Every callback is exception-isolated: a failing step is logged and the
   remaining steps still run, so the process exits cleanly with code 0
   instead of dying mid-cleanup with a traceback.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Awaitable, Callable

from loguru import logger

ShutdownCallback = Callable[[], Awaitable[None]]


class ShutdownManager:
    """Coordinates signal handling and ordered async cleanup callbacks."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._callbacks: list[tuple[str, ShutdownCallback]] = []
        self._reason: str | None = None
        self._completed = False

    @property
    def triggered(self) -> bool:
        """Whether shutdown has been requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """Why shutdown was requested (signal name, dashboard stop, …)."""
        return self._reason

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach SIGINT/SIGTERM handlers to ``loop``.

        On platforms without ``add_signal_handler`` (e.g. Windows event
        loops) this silently does nothing; ``KeyboardInterrupt`` handling in
        the entry point remains the fallback there.
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(
                    sig, self.request, f"received signal {sig.name}"
                )

    def request(self, reason: str = "shutdown requested") -> None:
        """Trip the shutdown event (idempotent; only the first reason sticks)."""
        if not self._event.is_set():
            self._reason = reason
            logger.info("Shutdown requested: {}", reason)
            self._event.set()

    async def wait(self) -> None:
        """Block until shutdown is requested."""
        await self._event.wait()

    def add_callback(self, name: str, callback: ShutdownCallback) -> None:
        """Register an async cleanup step; steps run in registration order."""
        self._callbacks.append((name, callback))

    async def run_callbacks(self) -> None:
        """Run all cleanup callbacks once, isolating failures per step."""
        if self._completed:
            return
        self._completed = True
        for name, callback in self._callbacks:
            try:
                await callback()
                logger.debug("Shutdown step ok: {}", name)
            except asyncio.CancelledError:  # never swallow cancellation
                raise
            except Exception as exc:
                logger.error("Shutdown step {!r} failed: {}", name, exc)
