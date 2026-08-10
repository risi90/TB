"""Grid trading strategy.

Builds a symmetric price grid around an anchor price (the first ticker seen).
Buy limit orders are placed on the grid levels below the market. When a buy at
level *i* fills, a matching sell limit is queued one level up, capturing the
grid spacing as profit; when that sell fills, the buy level is re-armed. The
strategy is long-only and never sells inventory it did not buy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from loguru import logger

from engine.models import (
    Order,
    OrderBook,
    OrderRequest,
    OrderSide,
    OrderType,
    Ticker,
)
from strategies.base import BaseStrategy


class _LevelState(Enum):
    """Lifecycle of one grid level."""

    IDLE = auto()          # ready to (re)place a buy order
    BUY_PENDING = auto()   # buy limit submitted, waiting for fill
    SELL_PENDING = auto()  # buy filled, matching sell submitted


@dataclass(slots=True)
class _GridLevel:
    """One rung of the grid: a buy price and its paired sell price."""

    buy_price: float
    sell_price: float
    state: _LevelState = _LevelState.IDLE
    buy_order_id: str | None = None
    sell_order_id: str | None = None
    amount: float = 0.0


class GridTradingStrategy(BaseStrategy):
    """Symmetric long-only grid around the first observed price.

    Args:
        symbol: Market to trade, e.g. ``"BTC/EUR"``.
        levels: Number of buy rungs below the anchor price.
        spacing_pct: Distance between adjacent rungs as a fraction (0.005 = 0.5%).
        order_quote_size: Quote-currency value of each rung's buy order.
    """

    def __init__(
        self,
        symbol: str,
        levels: int = 5,
        spacing_pct: float = 0.005,
        order_quote_size: float = 100.0,
    ) -> None:
        super().__init__(symbol)
        if levels < 1:
            raise ValueError("levels must be >= 1")
        if not 0 < spacing_pct < 0.5:
            raise ValueError("spacing_pct must be in (0, 0.5)")
        self._levels_count = levels
        self._spacing_pct = spacing_pct
        self._order_quote_size = order_quote_size
        self._anchor_price: float | None = None
        self._grid: list[_GridLevel] = []
        self._pending_requests: list[OrderRequest] = []

    @property
    def grid(self) -> list[_GridLevel]:
        """The current grid levels (read-only view for tests / inspection)."""
        return self._grid

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    async def on_ticker(self, ticker: Ticker) -> None:
        """Initialise the grid on the first ticker, then re-arm idle levels."""
        if self._anchor_price is None:
            self._build_grid(ticker.mid)
        self._arm_idle_levels()

    async def on_orderbook(self, orderbook: OrderBook) -> None:
        """Order book flow is not used by this strategy."""

    def generate_signals(self) -> list[OrderRequest]:
        """Emit queued grid orders exactly once each."""
        signals, self._pending_requests = self._pending_requests, []
        return signals

    async def on_fill(self, order: Order) -> None:
        """Advance the level state machine on buy/sell fills."""
        for level in self._grid:
            if order.side is OrderSide.BUY and order.id == level.buy_order_id:
                level.state = _LevelState.SELL_PENDING
                level.amount = order.filled
                sell = OrderRequest(
                    symbol=self.symbol,
                    side=OrderSide.SELL,
                    type=OrderType.LIMIT,
                    amount=order.filled,
                    price=level.sell_price,
                    reduce_only=True,
                )
                self._pending_requests.append(sell)
                logger.info(
                    "[{}] grid buy filled @ {:.2f}; queuing sell @ {:.2f}",
                    self.name, level.buy_price, level.sell_price,
                )
                return
            if order.side is OrderSide.SELL and order.id == level.sell_order_id:
                logger.info(
                    "[{}] grid cycle complete @ level {:.2f} -> {:.2f}",
                    self.name, level.buy_price, level.sell_price,
                )
                level.state = _LevelState.IDLE
                level.buy_order_id = None
                level.sell_order_id = None
                level.amount = 0.0
                return

    def register_order(self, request: OrderRequest, order: Order) -> None:
        """Bind an accepted order back to its grid level.

        Called by the trading loop after the router accepts a request, so the
        strategy can track fills for the right rung.
        """
        for level in self._grid:
            if (
                order.side is OrderSide.BUY
                and level.state is _LevelState.BUY_PENDING
                and level.buy_order_id is None
                and request.price == level.buy_price
            ):
                level.buy_order_id = order.id
                return
            if (
                order.side is OrderSide.SELL
                and level.state is _LevelState.SELL_PENDING
                and level.sell_order_id is None
                and request.price == level.sell_price
            ):
                level.sell_order_id = order.id
                return

    def on_order_rejected(self, request: OrderRequest) -> None:
        """Roll a level back to IDLE when its order was rejected downstream."""
        for level in self._grid:
            if (
                level.state is _LevelState.BUY_PENDING
                and level.buy_order_id is None
                and request.price == level.buy_price
            ):
                level.state = _LevelState.IDLE
                return

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build_grid(self, anchor_price: float) -> None:
        self._anchor_price = anchor_price
        self._grid = []
        for i in range(1, self._levels_count + 1):
            buy_price = anchor_price * (1.0 - self._spacing_pct * i)
            sell_price = anchor_price * (1.0 - self._spacing_pct * (i - 1))
            self._grid.append(_GridLevel(buy_price=buy_price, sell_price=sell_price))
        logger.info(
            "[{}] grid built around {:.2f}: {} levels, spacing {:.2%}",
            self.name, anchor_price, self._levels_count, self._spacing_pct,
        )

    def _arm_idle_levels(self) -> None:
        for level in self._grid:
            if level.state is not _LevelState.IDLE:
                continue
            amount = self._order_quote_size / level.buy_price
            self._pending_requests.append(
                OrderRequest(
                    symbol=self.symbol,
                    side=OrderSide.BUY,
                    type=OrderType.LIMIT,
                    amount=amount,
                    price=level.buy_price,
                )
            )
            level.state = _LevelState.BUY_PENDING
