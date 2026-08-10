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
    cycles: int = 0


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
        self._total_cycles = 0

    @property
    def strategy_id(self) -> str:
        """Stable persistence identifier, e.g. ``"grid:BTC/EUR"``."""
        return f"grid:{self.symbol}"

    @property
    def grid(self) -> list[_GridLevel]:
        """The current grid levels (read-only view for tests / inspection)."""
        return self._grid

    @property
    def total_cycles(self) -> int:
        """Completed buy→sell grid cycles across all levels."""
        return self._total_cycles

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
                level.cycles += 1
                self._total_cycles += 1
                logger.info(
                    "[{}] grid cycle #{} complete @ level {:.2f} -> {:.2f}",
                    self.name, self._total_cycles, level.buy_price, level.sell_price,
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

    def on_order_canceled(self, order: Order) -> None:
        """Reset the level bound to a canceled order.

        Used e.g. when a protective stop-loss exit cancels resting grid sells:
        the inventory is gone, so the level returns to IDLE and re-arms.
        """
        for level in self._grid:
            if order.id == level.buy_order_id:
                level.state = _LevelState.IDLE
                level.buy_order_id = None
                return
            if order.id == level.sell_order_id:
                level.state = _LevelState.IDLE
                level.sell_order_id = None
                level.amount = 0.0
                return

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------
    def to_state(self) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Serialize the grid for persistence.

        Returns:
            ``(config, levels)`` — the grid's configuration (including anchor
            price and cycle totals) and one dict per level, both
            JSON-serializable.
        """
        config: dict[str, object] = {
            "levels": self._levels_count,
            "spacing_pct": self._spacing_pct,
            "order_quote_size": self._order_quote_size,
            "anchor_price": self._anchor_price,
            "total_cycles": self._total_cycles,
        }
        levels: list[dict[str, object]] = [
            {
                "buy_price": level.buy_price,
                "sell_price": level.sell_price,
                "state": level.state.name,
                "buy_order_id": level.buy_order_id,
                "sell_order_id": level.sell_order_id,
                "amount": level.amount,
                "cycles": level.cycles,
            }
            for level in self._grid
        ]
        return config, levels

    def matches_config(self, config: dict[str, object]) -> bool:
        """Whether a persisted config is compatible with this instance's
        parameters (level count, spacing, and order size all unchanged)."""
        try:
            return (
                int(config["levels"]) == self._levels_count  # type: ignore[arg-type]
                and float(config["spacing_pct"]) == self._spacing_pct  # type: ignore[arg-type]
                and float(config["order_quote_size"]) == self._order_quote_size  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError):
            return False

    def restore_state(
        self,
        config: dict[str, object],
        levels: list[dict[str, object]],
        open_order_ids: set[str] | None = None,
    ) -> None:
        """Rehydrate the grid from persisted state after a restart.

        Args:
            config: The persisted config from :meth:`to_state`.
            levels: The persisted level dicts from :meth:`to_state`.
            open_order_ids: Ids of orders actually still open in the engine.
                Levels referencing orders that no longer exist are repaired:
                a lost buy resets the level to IDLE (re-armed next tick); a
                lost sell for held inventory is re-queued.
        """
        anchor = config.get("anchor_price")
        if anchor is None:
            logger.warning("[{}] persisted grid has no anchor; rebuilding fresh", self.name)
            return
        self._anchor_price = float(anchor)  # type: ignore[arg-type]
        self._total_cycles = int(config.get("total_cycles", 0))  # type: ignore[arg-type]
        self._grid = []
        for row in levels:
            level = _GridLevel(
                buy_price=float(row["buy_price"]),  # type: ignore[arg-type]
                sell_price=float(row["sell_price"]),  # type: ignore[arg-type]
                state=_LevelState[str(row["state"])],
                buy_order_id=(
                    str(row["buy_order_id"]) if row.get("buy_order_id") else None
                ),
                sell_order_id=(
                    str(row["sell_order_id"]) if row.get("sell_order_id") else None
                ),
                amount=float(row.get("amount") or 0.0),  # type: ignore[arg-type]
                cycles=int(row.get("cycles") or 0),  # type: ignore[arg-type]
            )
            if open_order_ids is not None:
                self._repair_level(level, open_order_ids)
            self._grid.append(level)
        logger.info(
            "[{}] grid restored: anchor {:.2f}, {} levels, {} cycles completed",
            self.name, self._anchor_price, len(self._grid), self._total_cycles,
        )

    def _repair_level(self, level: _GridLevel, open_order_ids: set[str]) -> None:
        """Fix levels whose persisted orders are no longer open in the engine."""
        if level.state is _LevelState.BUY_PENDING and (
            level.buy_order_id is None or level.buy_order_id not in open_order_ids
        ):
            level.state = _LevelState.IDLE
            level.buy_order_id = None
        elif level.state is _LevelState.SELL_PENDING and (
            level.sell_order_id is None or level.sell_order_id not in open_order_ids
        ):
            level.sell_order_id = None
            if level.amount > 0:
                # Inventory is held but its sell order vanished — re-queue it.
                self._pending_requests.append(
                    OrderRequest(
                        symbol=self.symbol,
                        side=OrderSide.SELL,
                        type=OrderType.LIMIT,
                        amount=level.amount,
                        price=level.sell_price,
                        reduce_only=True,
                    )
                )
            else:
                level.state = _LevelState.IDLE

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
