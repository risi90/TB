"""Abstract base class all trading strategies must implement.

A strategy is a pure signal generator: it consumes market data events
(:meth:`on_ticker`, :meth:`on_orderbook`) and emits
:class:`~engine.models.OrderRequest` objects from :meth:`generate_signals`.
It never talks to an exchange or the paper engine directly — every request it
produces is vetted by the :class:`~risk.risk_manager.RiskManager` and executed
by the :class:`~engine.execution.ExecutionRouter`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from engine.models import Order, OrderBook, OrderRequest, Ticker


class BaseStrategy(ABC):
    """Contract for event-driven trading strategies.

    Args:
        symbol: The market this strategy instance trades, e.g. ``"BTC/EUR"``.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @property
    def name(self) -> str:
        """Human-readable strategy name for logging."""
        return type(self).__name__

    @abstractmethod
    async def on_ticker(self, ticker: Ticker) -> None:
        """Consume a real-time ticker update for :attr:`symbol`."""

    @abstractmethod
    async def on_orderbook(self, orderbook: OrderBook) -> None:
        """Consume a real-time order book update for :attr:`symbol`."""

    @abstractmethod
    def generate_signals(self) -> list[OrderRequest]:
        """Return the orders the strategy wants to place right now.

        Called by the trading loop after each market data event. Must be
        idempotent between fills: repeated calls without new information
        should not emit duplicate orders.
        """

    async def on_fill(self, order: Order) -> None:  # noqa: B027
        """Notification that one of this strategy's orders was filled.

        Optional hook; the default implementation does nothing.
        """
