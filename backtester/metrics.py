"""Performance metrics computed from backtest results.

All functions are pure and operate on plain sequences so they can be tested
against hand-computed expectations without running a full backtest.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Sequence

from backtester.models import BacktestResult, TradeRecord, timeframe_to_ms

_SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass(slots=True)
class BacktestMetrics:
    """Key performance statistics of one backtest run."""

    total_pnl: float
    total_return_pct: float
    benchmark_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    max_drawdown_duration_s: float
    sharpe_ratio: float
    sortino_ratio: float
    win_rate_pct: float
    profit_factor: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_fees: float
    final_equity: float

    def to_dict(self) -> dict[str, float | int]:
        """Plain-dict view (e.g. for tables or JSON)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
def periodic_returns(equity: Sequence[float]) -> list[float]:
    """Simple per-period returns of an equity curve."""
    return [
        equity[i] / equity[i - 1] - 1.0
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]


def max_drawdown(
    equity: Sequence[float], timestamps: Sequence[float] | None = None
) -> tuple[float, float]:
    """Maximum peak-to-trough drawdown of an equity curve.

    Returns:
        ``(depth, duration_seconds)`` where ``depth`` is a positive fraction
        (0.25 = −25%) and ``duration_seconds`` spans from the peak preceding
        the deepest trough until equity first recovers to that peak (or the
        end of the series if it never does). Duration is 0.0 when no
        timestamps are provided or there is no drawdown.
    """
    if len(equity) < 2:
        return 0.0, 0.0

    peak_idx = 0
    max_depth = 0.0
    max_peak_idx = 0
    for i, value in enumerate(equity):
        if value > equity[peak_idx]:
            peak_idx = i
        elif equity[peak_idx] > 0:
            depth = 1.0 - value / equity[peak_idx]
            if depth > max_depth:
                max_depth = depth
                max_peak_idx = peak_idx

    if max_depth == 0.0 or timestamps is None:
        return max_depth, 0.0

    peak_value = equity[max_peak_idx]
    recovery_idx = len(equity) - 1
    for i in range(max_peak_idx + 1, len(equity)):
        if equity[i] >= peak_value:
            recovery_idx = i
            break
    return max_depth, float(timestamps[recovery_idx] - timestamps[max_peak_idx])


def sharpe_ratio(
    returns: Sequence[float], periods_per_year: float, risk_free_rate: float = 0.0
) -> float:
    """Annualized Sharpe ratio (0.0 when volatility is zero or data is short)."""
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free_rate / periods_per_year for r in returns]
    stdev = statistics.stdev(excess)
    if stdev == 0:
        return 0.0
    return statistics.mean(excess) / stdev * math.sqrt(periods_per_year)


def sortino_ratio(
    returns: Sequence[float], periods_per_year: float, risk_free_rate: float = 0.0
) -> float:
    """Annualized Sortino ratio using downside deviation.

    Returns ``inf`` when there is no downside and the mean return is
    positive, 0.0 when there is no downside and no gains either.
    """
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free_rate / periods_per_year for r in returns]
    downside_sq = [min(r, 0.0) ** 2 for r in excess]
    downside_dev = math.sqrt(sum(downside_sq) / len(excess))
    mean = statistics.mean(excess)
    if downside_dev == 0:
        return math.inf if mean > 0 else 0.0
    return mean / downside_dev * math.sqrt(periods_per_year)


def cagr(
    initial: float, final: float, duration_seconds: float
) -> float:
    """Compound annual growth rate as a fraction (0.10 = 10%/year)."""
    if initial <= 0 or final <= 0 or duration_seconds <= 0:
        return 0.0
    years = duration_seconds / _SECONDS_PER_YEAR
    return (final / initial) ** (1.0 / years) - 1.0


def trade_stats(trades: Sequence[TradeRecord]) -> tuple[float, float, int, int]:
    """Win rate %, profit factor, winning and losing closed-trade counts.

    A "closed trade" is a sell fill; its ``realized_pnl`` — which the engines
    compute **net of all fees and slippage** (sell fee plus the proportional
    entry fees of the closed amount) — decides win/loss, so a trade that only
    wins gross of costs counts as a loss. Profit factor is net profit / net
    loss (``inf`` with no losses).
    """
    closed = [t for t in trades if t.side == "sell"]
    wins = [t for t in closed if t.realized_pnl > 0]
    losses = [t for t in closed if t.realized_pnl < 0]
    win_rate = 100.0 * len(wins) / len(closed) if closed else 0.0
    gross_profit = sum(t.realized_pnl for t in wins)
    gross_loss = -sum(t.realized_pnl for t in losses)
    if gross_loss == 0:
        profit_factor = math.inf if gross_profit > 0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss
    return win_rate, profit_factor, len(wins), len(losses)


# ---------------------------------------------------------------------------
# Top-level aggregation
# ---------------------------------------------------------------------------
def compute_metrics(result: BacktestResult) -> BacktestMetrics:
    """Aggregate all performance statistics for one backtest result."""
    initial = result.initial_capital
    final = result.final_equity
    total_pnl = final - initial
    total_return_pct = 100.0 * total_pnl / initial if initial else 0.0

    benchmark_return_pct = 0.0
    if result.benchmark:
        benchmark_return_pct = 100.0 * (result.benchmark[-1] / initial - 1.0)

    timeframe_seconds = timeframe_to_ms(result.timeframe) / 1000.0
    periods_per_year = _SECONDS_PER_YEAR / timeframe_seconds
    returns = periodic_returns(result.equity)
    dd_pct, dd_duration = max_drawdown(result.equity, result.timestamps)
    win_rate, profit_factor, wins, losses = trade_stats(result.trades)

    return BacktestMetrics(
        total_pnl=total_pnl,
        total_return_pct=total_return_pct,
        benchmark_return_pct=benchmark_return_pct,
        cagr_pct=100.0 * cagr(initial, final, result.duration_seconds),
        max_drawdown_pct=100.0 * dd_pct,
        max_drawdown_duration_s=dd_duration,
        sharpe_ratio=sharpe_ratio(returns, periods_per_year),
        sortino_ratio=sortino_ratio(returns, periods_per_year),
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        total_trades=len(result.trades),
        winning_trades=wins,
        losing_trades=losses,
        total_fees=result.total_fees,
        final_equity=final,
    )
