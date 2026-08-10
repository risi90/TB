"""Shared backtesting data structures and timeframe helpers.

``Candle`` is an alias of :class:`engine.candle_aggregator.Bar` — the exact
type live strategies receive in ``on_bar_close`` — so a backtested strategy
consumes byte-identical inputs to its live counterpart. The timeframe table
is likewise shared with the live candle aggregator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from engine.candle_aggregator import (  # noqa: F401  (re-exported)
    TIMEFRAMES_MS,
    Bar,
    timeframe_to_ms,
)

#: One OHLCV candle (identical to the live Bar type; timestamps in seconds).
Candle = Bar


@dataclass(slots=True)
class TradeRecord:
    """One executed fill during a backtest."""

    timestamp: float
    side: str
    type: str
    price: float
    amount: float
    fee: float
    realized_pnl: float = 0.0

    @property
    def cost(self) -> float:
        """Traded value in quote currency, excluding fees."""
        return self.amount * self.price


@dataclass(slots=True)
class BacktestResult:
    """Everything a backtest run produces, ready for metrics and plotting.

    ``timestamps``, ``equity``, ``benchmark`` and ``close_prices`` are
    parallel per-candle series; ``benchmark`` is the buy-&-hold equity of the
    same initial capital (taker fee applied on entry at the first open).
    """

    symbol: str
    timeframe: str
    initial_capital: float
    timestamps: list[float] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    benchmark: list[float] = field(default_factory=list)
    close_prices: list[float] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    total_fees: float = 0.0

    @property
    def final_equity(self) -> float:
        """Equity at the last processed candle (initial capital if empty)."""
        return self.equity[-1] if self.equity else self.initial_capital

    @property
    def duration_seconds(self) -> float:
        """Wall-clock span covered by the replayed candles."""
        if len(self.timestamps) < 2:
            return 0.0
        return self.timestamps[-1] - self.timestamps[0]


def drawdown_series(equity: Sequence[float]) -> list[float]:
    """Per-point drawdown fractions (<= 0) from the running equity peak."""
    result: list[float] = []
    peak = float("-inf")
    for value in equity:
        peak = max(peak, value)
        result.append(0.0 if peak <= 0 else value / peak - 1.0)
    return result
