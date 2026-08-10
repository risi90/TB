"""Shared backtesting data structures and timeframe helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

#: Supported CCXT timeframes and their duration in milliseconds.
TIMEFRAMES_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def timeframe_to_ms(timeframe: str) -> int:
    """Duration of one candle in milliseconds for a CCXT timeframe string."""
    try:
        return TIMEFRAMES_MS[timeframe]
    except KeyError:
        raise ValueError(
            f"unsupported timeframe {timeframe!r}; expected one of "
            f"{sorted(TIMEFRAMES_MS)}"
        ) from None


@dataclass(slots=True)
class Candle:
    """One OHLCV candle. ``timestamp`` is the candle open time in seconds."""

    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


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
