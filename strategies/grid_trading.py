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

from engine.candle_aggregator import Bar
from engine.models import (
    Order,
    OrderBook,
    OrderRequest,
    OrderSide,
    OrderType,
    Ticker,
)
from strategies.base import BaseStrategy
from strategies.regime import Regime, RegimeFilter


class _LevelState(Enum):
    """Lifecycle of one grid level."""

    IDLE = auto()          # ready to (re)place a buy order
    BUY_PENDING = auto()   # buy limit submitted, waiting for fill
    SELL_PENDING = auto()  # buy filled, matching sell submitted


@dataclass(slots=True)
class _GridLevel:
    """One rung of the grid: a buy price and its paired sell price.

    ``stale`` marks levels carried over from a previous anchor after a
    re-anchor: they keep managing their held inventory (the resting sell)
    but are removed once the cycle completes instead of re-arming.
    """

    buy_price: float
    sell_price: float
    state: _LevelState = _LevelState.IDLE
    buy_order_id: str | None = None
    sell_order_id: str | None = None
    amount: float = 0.0
    cycles: int = 0
    stale: bool = False


class GridTradingStrategy(BaseStrategy):
    """Symmetric long-only grid around the first observed price.

    Args:
        symbol: Market to trade, e.g. ``"BTC/EUR"``.
        levels: Number of buy rungs below the anchor price.
        spacing_pct: Distance between adjacent rungs as a fraction (0.005 = 0.5%).
        order_quote_size: Quote-currency value of each rung's buy order.
        auto_reanchor: Re-center the grid when price drifts further than
            ``reanchor_factor × grid width`` from the anchor. Resting unfilled
            buys are canceled and fresh rungs are built around the new price;
            levels holding inventory keep their sells and retire once sold.
        reanchor_factor: Drift threshold in grid-width multiples.
        max_inventory_quote: Hard cap on committed capital (held inventory at
            entry value plus resting buys), in quote currency. ``None`` uses
            one full grid (``levels × order_quote_size``) — this matters after
            downward re-anchors, which would otherwise stack inventory
            without bound on a falling market.
        aligned_protection: Stamp grid-aware protective levels on entries:
            the stop-loss sits ``stop_loss_buffer_pct`` **below the bottom of
            the whole grid** (instead of below each individual entry, which
            turns the dips the grid is designed to buy into realized losses)
            and the take-profit sits ``take_profit_buffer_pct`` above the
            anchor. Disabled, entries carry no levels and the risk manager
            stamps its per-entry defaults.
        stop_loss_buffer_pct: Distance of the stop below the lowest rung.
        take_profit_buffer_pct: Distance of the take-profit above the anchor.
        regime_filter: Optional trend detector fed with completed bar closes
            (``on_bar_close``). While it reports ``TRENDING_DOWN`` the grid
            suspends new entries and cancels resting buys — a long-only grid
            buying into a falling trend is its main way of losing money.
            Sells (inventory management) always stay active. ``None`` (the
            default) disables the filter.
    """

    def __init__(
        self,
        symbol: str,
        levels: int = 5,
        spacing_pct: float = 0.005,
        order_quote_size: float = 100.0,
        auto_reanchor: bool = True,
        reanchor_factor: float = 1.5,
        max_inventory_quote: float | None = None,
        aligned_protection: bool = True,
        stop_loss_buffer_pct: float = 0.02,
        take_profit_buffer_pct: float = 0.04,
        regime_filter: RegimeFilter | None = None,
    ) -> None:
        super().__init__(symbol)
        if levels < 1:
            raise ValueError("levels must be >= 1")
        if not 0 < spacing_pct < 0.5:
            raise ValueError("spacing_pct must be in (0, 0.5)")
        if reanchor_factor <= 0:
            raise ValueError("reanchor_factor must be positive")
        if max_inventory_quote is not None and max_inventory_quote <= 0:
            raise ValueError("max_inventory_quote must be positive (or None)")
        self._levels_count = levels
        self._spacing_pct = spacing_pct
        self._order_quote_size = order_quote_size
        self._auto_reanchor = auto_reanchor
        self._reanchor_factor = reanchor_factor
        self._max_inventory_quote = max_inventory_quote
        self._aligned_protection = aligned_protection
        self._stop_loss_buffer_pct = stop_loss_buffer_pct
        self._take_profit_buffer_pct = take_profit_buffer_pct
        self._regime_filter = regime_filter
        self._anchor_price: float | None = None
        self._grid: list[_GridLevel] = []
        self._pending_requests: list[OrderRequest] = []
        self._pending_cancel_ids: list[str] = []
        self._total_cycles = 0
        self._reanchor_count = 0

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

    @property
    def reanchor_count(self) -> int:
        """How many times the grid has re-centered around a new price."""
        return self._reanchor_count

    @property
    def spacing_pct(self) -> float:
        """Distance between adjacent rungs as a fraction."""
        return self._spacing_pct

    @property
    def inventory_cap_quote(self) -> float:
        """Effective committed-capital cap in quote currency."""
        if self._max_inventory_quote is not None:
            return self._max_inventory_quote
        return self._levels_count * self._order_quote_size

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    async def on_ticker(self, ticker: Ticker) -> None:
        """Initialise the grid on the first ticker, re-anchor on large drift,
        then (re-)arm idle levels within the inventory cap."""
        if self._anchor_price is None:
            self._build_grid(ticker.mid)
        elif self._auto_reanchor and self._should_reanchor(ticker.mid):
            self._reanchor(ticker.mid)
        self._arm_idle_levels()

    async def on_orderbook(self, orderbook: OrderBook) -> None:
        """Order book flow is not used by this strategy."""

    async def on_bar_close(self, bar: Bar) -> None:
        """Feed the regime filter and suspend/resume entries on transitions."""
        if self._regime_filter is None:
            return
        before = self._regime_filter.regime
        after = self._regime_filter.update(bar.close)
        if after is before:
            return
        logger.info(
            "[{}] regime {} -> {} (efficiency ratio {:.2f})",
            self.name, before, after, self._regime_filter.efficiency_ratio,
        )
        if after is Regime.TRENDING_DOWN:
            self._suspend_entries()

    @property
    def entries_allowed(self) -> bool:
        """Whether new grid buys may be armed under the current regime."""
        return (
            self._regime_filter is None
            or self._regime_filter.regime is not Regime.TRENDING_DOWN
        )

    def _suspend_entries(self) -> None:
        """Cancel resting buys and drop queued entry requests (trend defense)."""
        canceled = 0
        for level in self._grid:
            if level.state is _LevelState.BUY_PENDING:
                if level.buy_order_id:
                    self._pending_cancel_ids.append(level.buy_order_id)
                    canceled += 1
                level.state = _LevelState.IDLE
                level.buy_order_id = None
        self._pending_requests = [
            r for r in self._pending_requests
            if r.side is not OrderSide.BUY or r.reduce_only
        ]
        logger.warning(
            "[{}] downtrend detected — entries suspended, {} resting buys "
            "canceled (sells stay active)",
            self.name, canceled,
        )

    def generate_signals(self) -> list[OrderRequest]:
        """Emit queued grid orders exactly once each."""
        signals, self._pending_requests = self._pending_requests, []
        return signals

    def generate_cancellations(self) -> list[str]:
        """Emit ids of resting orders obsoleted by a re-anchor, exactly once."""
        ids, self._pending_cancel_ids = self._pending_cancel_ids, []
        return ids

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
                if level.stale:  # off-grid inventory sold: retire the level
                    self._grid.remove(level)
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
            "reanchor_count": self._reanchor_count,
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
                "stale": level.stale,
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
        self._reanchor_count = int(config.get("reanchor_count", 0))  # type: ignore[arg-type]
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
                stale=bool(row.get("stale", False)),
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
    def _fresh_levels(self, anchor_price: float) -> list[_GridLevel]:
        return [
            _GridLevel(
                buy_price=anchor_price * (1.0 - self._spacing_pct * i),
                sell_price=anchor_price * (1.0 - self._spacing_pct * (i - 1)),
            )
            for i in range(1, self._levels_count + 1)
        ]

    def _build_grid(self, anchor_price: float) -> None:
        self._anchor_price = anchor_price
        self._grid = self._fresh_levels(anchor_price)
        logger.info(
            "[{}] grid built around {:.2f}: {} levels, spacing {:.2%}",
            self.name, anchor_price, self._levels_count, self._spacing_pct,
        )

    def _grid_width(self) -> float:
        assert self._anchor_price is not None
        return self._anchor_price * self._spacing_pct * self._levels_count

    def _should_reanchor(self, price: float) -> bool:
        assert self._anchor_price is not None
        return abs(price - self._anchor_price) > self._reanchor_factor * self._grid_width()

    def _reanchor(self, new_anchor: float) -> None:
        """Re-center the grid: cancel resting buys, keep inventory levels."""
        for level in self._grid:
            if level.state is _LevelState.BUY_PENDING and level.buy_order_id:
                self._pending_cancel_ids.append(level.buy_order_id)
        kept = [lvl for lvl in self._grid if lvl.state is _LevelState.SELL_PENDING]
        for level in kept:
            level.stale = True
        self._reanchor_count += 1
        logger.info(
            "[{}] re-anchor #{}: {:.2f} -> {:.2f} (drift > {:.1f}x grid width); "
            "canceling {} resting buys, keeping {} inventory levels",
            self.name, self._reanchor_count, self._anchor_price, new_anchor,
            self._reanchor_factor, len(self._pending_cancel_ids), len(kept),
        )
        self._anchor_price = new_anchor
        self._grid = self._fresh_levels(new_anchor) + kept

    def _committed_quote(self) -> float:
        """Capital committed to the grid: resting buys at order size plus
        held inventory valued at its entry price."""
        total = 0.0
        for level in self._grid:
            if level.state is _LevelState.BUY_PENDING:
                total += self._order_quote_size
            elif level.state is _LevelState.SELL_PENDING:
                total += level.amount * level.buy_price
        return total

    def grid_floor_price(self) -> float | None:
        """Price of the lowest (fresh) rung, or ``None`` before the grid exists."""
        if self._anchor_price is None:
            return None
        return self._anchor_price * (1.0 - self._spacing_pct * self._levels_count)

    def _protective_levels(self) -> tuple[float | None, float | None]:
        """Grid-aware (stop_loss, take_profit) for new entries, if enabled."""
        floor = self.grid_floor_price()
        if not self._aligned_protection or floor is None:
            return None, None
        assert self._anchor_price is not None
        return (
            floor * (1.0 - self._stop_loss_buffer_pct),
            self._anchor_price * (1.0 + self._take_profit_buffer_pct),
        )

    def _arm_idle_levels(self) -> None:
        if not self.entries_allowed:
            return
        cap = self.inventory_cap_quote
        committed = self._committed_quote()
        stop_loss, take_profit = self._protective_levels()
        for level in self._grid:
            if level.state is not _LevelState.IDLE or level.stale:
                continue
            if committed + self._order_quote_size > cap + 1e-9:
                logger.debug(
                    "[{}] inventory cap {:.2f} reached; level {:.2f} stays idle",
                    self.name, cap, level.buy_price,
                )
                continue
            amount = self._order_quote_size / level.buy_price
            self._pending_requests.append(
                OrderRequest(
                    symbol=self.symbol,
                    side=OrderSide.BUY,
                    type=OrderType.LIMIT,
                    amount=amount,
                    price=level.buy_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
            )
            level.state = _LevelState.BUY_PENDING
            committed += self._order_quote_size
