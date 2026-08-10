"""Simple SMA crossover trend-following strategy.

Maintains fast and slow simple moving averages over **completed bar closes**
(never raw ticks — tick rates are irregular, which would distort the
indicator and diverge from backtests). A fast-over-slow crossover emits a
market buy; a fast-under-slow crossunder emits a market sell that closes the
position. Intended as a minimal, easily-testable reference strategy.
"""

from __future__ import annotations

from collections import deque

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


class SmaCrossoverStrategy(BaseStrategy):
    """Golden-cross / death-cross strategy on completed OHLCV bars.

    Args:
        symbol: Market to trade, e.g. ``"BTC/EUR"``.
        fast_period: Window of the fast SMA (bars).
        slow_period: Window of the slow SMA (bars); must exceed ``fast_period``.
        order_quote_size: Quote value of each entry order.
    """

    def __init__(
        self,
        symbol: str,
        fast_period: int = 10,
        slow_period: int = 30,
        order_quote_size: float = 250.0,
    ) -> None:
        super().__init__(symbol)
        if slow_period <= fast_period:
            raise ValueError("slow_period must be greater than fast_period")
        self._fast_prices: deque[float] = deque(maxlen=fast_period)
        self._slow_prices: deque[float] = deque(maxlen=slow_period)
        self._order_quote_size = order_quote_size
        self._prev_diff: float | None = None
        self._position_amount: float = 0.0
        self._pending_requests: list[OrderRequest] = []

    # ------------------------------------------------------------------
    # Indicators
    # ------------------------------------------------------------------
    @property
    def fast_sma(self) -> float | None:
        """Fast SMA, or ``None`` until its window is full."""
        if len(self._fast_prices) < self._fast_prices.maxlen:  # type: ignore[operator]
            return None
        return sum(self._fast_prices) / len(self._fast_prices)

    @property
    def slow_sma(self) -> float | None:
        """Slow SMA, or ``None`` until its window is full."""
        if len(self._slow_prices) < self._slow_prices.maxlen:  # type: ignore[operator]
            return None
        return sum(self._slow_prices) / len(self._slow_prices)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    async def on_ticker(self, ticker: Ticker) -> None:
        """Ticks are intentionally ignored — indicators use bar closes only."""

    async def on_orderbook(self, orderbook: OrderBook) -> None:
        """Order book flow is not used by this strategy."""

    async def on_bar_close(self, bar: Bar) -> None:
        """Update the SMAs with the completed bar close and detect crossovers."""
        self._fast_prices.append(bar.close)
        self._slow_prices.append(bar.close)

        fast, slow = self.fast_sma, self.slow_sma
        if fast is None or slow is None:
            return

        diff = fast - slow
        if self._prev_diff is not None:
            if self._prev_diff <= 0 < diff and self._position_amount == 0:
                amount = self._order_quote_size / bar.close
                self._pending_requests.append(
                    OrderRequest(
                        symbol=self.symbol,
                        side=OrderSide.BUY,
                        type=OrderType.MARKET,
                        amount=amount,
                    )
                )
                logger.info(
                    "[{}] golden cross (fast {:.2f} > slow {:.2f}) -> BUY",
                    self.name, fast, slow,
                )
            elif self._prev_diff >= 0 > diff and self._position_amount > 0:
                self._pending_requests.append(
                    OrderRequest(
                        symbol=self.symbol,
                        side=OrderSide.SELL,
                        type=OrderType.MARKET,
                        amount=self._position_amount,
                        reduce_only=True,
                    )
                )
                logger.info(
                    "[{}] death cross (fast {:.2f} < slow {:.2f}) -> SELL",
                    self.name, fast, slow,
                )
        self._prev_diff = diff

    def generate_signals(self) -> list[OrderRequest]:
        """Emit pending crossover orders exactly once each."""
        signals, self._pending_requests = self._pending_requests, []
        return signals

    async def on_fill(self, order: Order) -> None:
        """Track the strategy's net position from fills."""
        if order.side is OrderSide.BUY:
            self._position_amount += order.filled
        else:
            self._position_amount = max(0.0, self._position_amount - order.filled)

    def sync_position(self, amount: float) -> None:
        """Seed the tracked position, e.g. from a restored engine position."""
        self._position_amount = max(0.0, amount)
