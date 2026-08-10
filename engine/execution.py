"""Order routing: paper engine by default, live exchange only when explicitly armed.

This module is the safety choke point. Every order the bot wants to place goes
through :meth:`ExecutionRouter.submit`, which routes to the
:class:`~engine.paper_engine.PaperEngine` unless live trading has been
explicitly enabled (``PAPER_TRADING=False`` **and** API keys present). Any
attempt to trade live without that raises :class:`LiveTradingBlocked` — there
is no code path that silently sends a real order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from config.config import Settings
from engine.models import Fill, Order, OrderRequest
from engine.paper_engine import PaperEngine

if TYPE_CHECKING:
    from connectors.base import BaseConnector


class LiveTradingBlocked(Exception):
    """Raised when a live order is attempted without explicit authorization."""


class ExecutionRouter:
    """Routes validated orders to the paper engine or the live exchange.

    Args:
        settings: Application settings (source of the paper/live decision).
        paper_engine: The simulated account used in paper mode.
        connector: Live exchange connector; only used when live trading is
            explicitly enabled. May be ``None`` in paper-only setups.
    """

    def __init__(
        self,
        settings: Settings,
        paper_engine: PaperEngine,
        connector: "BaseConnector | None" = None,
    ) -> None:
        self._settings = settings
        self._paper_engine = paper_engine
        self._connector = connector

    @property
    def is_paper(self) -> bool:
        """Whether orders are currently routed to the paper engine."""
        return not self._settings.live_trading_allowed()

    async def submit(self, request: OrderRequest) -> tuple[Order, list[Fill]]:
        """Execute a validated order request.

        Returns:
            The resulting order and any immediate fills (paper mode). In live
            mode the fills list is empty; fills arrive via exchange streams.

        Raises:
            LiveTradingBlocked: If paper mode is off but the configuration
                does not fully authorize live trading.
        """
        if self._settings.paper_trading:
            return self._paper_engine.create_order(request)

        # --- live path: hard safety gate -----------------------------
        if not self._settings.live_trading_allowed():
            raise LiveTradingBlocked(
                "PAPER_TRADING is False but API credentials are missing or "
                "invalid. Refusing to send a real order."
            )
        if self._connector is None:
            raise LiveTradingBlocked(
                "Live trading enabled but no exchange connector is configured."
            )

        logger.warning(
            "LIVE ORDER: {} {} {:.8f} {} @ {}",
            request.side.upper(),
            request.type,
            request.amount,
            request.symbol,
            request.price if request.price is not None else "market",
        )
        order = await self._connector.create_order(request)
        return order, []

    async def cancel(self, order_id: str, symbol: str) -> None:
        """Cancel an order on whichever venue currently owns it."""
        if self._settings.paper_trading:
            self._paper_engine.cancel_order(order_id)
            return
        if not self._settings.live_trading_allowed() or self._connector is None:
            raise LiveTradingBlocked("Refusing live cancel without authorization.")
        await self._connector.cancel_order(order_id, symbol)
