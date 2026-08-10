"""Abstract exchange connector interface.

Keeping the interface abstract lets the rest of the framework (engine,
strategies, risk) stay completely independent of CCXT, which also makes unit
testing trivial — tests drive the paper engine with synthetic tickers instead
of a live connection.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from engine.models import Order, OrderBook, OrderRequest, Ticker

TickerHandler = Callable[[Ticker], Awaitable[None]]
OrderBookHandler = Callable[[OrderBook], Awaitable[None]]


class BaseConnector(ABC):
    """Contract for asynchronous exchange connectivity."""

    @abstractmethod
    async def watch_ticker(self, symbol: str, handler: TickerHandler) -> None:
        """Stream ticker updates for ``symbol`` into ``handler`` forever.

        Implementations must survive disconnects and rate limits by
        reconnecting with exponential backoff; they only exit on
        cancellation or :meth:`close`.
        """

    @abstractmethod
    async def watch_order_book(self, symbol: str, handler: OrderBookHandler) -> None:
        """Stream order book updates for ``symbol`` into ``handler`` forever."""

    @abstractmethod
    async def create_order(self, request: OrderRequest) -> Order:
        """Place a real order on the exchange (live mode only)."""

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> None:
        """Cancel a real order on the exchange (live mode only)."""

    @abstractmethod
    async def fetch_balance(self) -> dict[str, float]:
        """Fetch free balances per currency from the exchange."""

    @abstractmethod
    async def close(self) -> None:
        """Release all network resources (WebSocket + HTTP sessions)."""
