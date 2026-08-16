"""Grid parameter sweep with in-sample / out-of-sample validation.

Runs the backtest engine over a grid of (spacing × levels × stop-loss)
combinations, three times each: the full period, a training window (the
first ``train_fraction`` of candles) and a held-out test window (the rest).
A combination that is only profitable in-sample is overfit noise; candidates
worth attention are profitable in **both** windows.

Combinations whose spacing sits below the fee floor (``2 × maker fee +
0.1%``) are excluded up front rather than backtested — they lose money by
construction, and reporting them as data would only muddy the heatmap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
from typing import Callable, Sequence

from loguru import logger

from config.config import Settings
from backtester.engine import BacktestEngine
from backtester.metrics import compute_metrics
from backtester.models import Candle
from strategies.grid_trading import GridTradingStrategy
from strategies.regime import RegimeFilter

#: Modules whose per-fill INFO logging would flood a sweep of many runs.
_NOISY_MODULES = (
    "engine.paper_engine",
    "strategies.grid_trading",
    "backtester.engine",
    "risk.risk_manager",
)

ProgressCallback = Callable[[int, int], None]


@dataclass(slots=True)
class SweepResult:
    """Outcome of one parameter combination across all three windows."""

    spacing_pct: float
    levels: int
    stop_loss_pct: float
    viable: bool
    reason: str | None
    full_return_pct: float = 0.0
    train_return_pct: float = 0.0
    test_return_pct: float = 0.0
    benchmark_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    total_fees: float = 0.0

    @property
    def robust(self) -> bool:
        """Profitable in-sample AND out-of-sample (the bar that matters)."""
        return self.viable and self.train_return_pct > 0 and self.test_return_pct > 0

    def to_dict(self) -> dict[str, object]:
        """Plain-dict view including the derived ``robust`` flag."""
        return asdict(self) | {"robust": self.robust}


async def sweep_grid(
    candles: Sequence[Candle],
    symbol: str,
    timeframe: str,
    *,
    spacings: Sequence[float],
    levels_options: Sequence[int],
    stop_loss_pcts: Sequence[float],
    order_quote_size: float = 100.0,
    initial_capital: float = 10_000.0,
    maker_fee_rate: float = 0.0015,
    taker_fee_rate: float = 0.0025,
    slippage_rate: float = 0.0005,
    aligned_protection: bool = True,
    regime_filter: bool = True,
    train_fraction: float = 0.7,
    progress: ProgressCallback | None = None,
) -> list[SweepResult]:
    """Backtest every (spacing, levels, stop-loss) combination.

    Args:
        candles: Full historical series (split internally into train/test).
        stop_loss_pcts: Buffer below the whole grid when
            ``aligned_protection`` is on; per-entry stop fraction otherwise.
        train_fraction: Share of candles forming the in-sample window.
        progress: Optional ``callback(done, total)`` for UI progress bars.

    Returns:
        One :class:`SweepResult` per combination, robust candidates first,
        then by out-of-sample return descending.
    """
    if len(candles) < 20:
        raise ValueError("need at least 20 candles for a meaningful sweep")
    if not 0.1 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be between 0.1 and 0.9")

    split = int(len(candles) * train_fraction)
    fee_floor = 2.0 * maker_fee_rate + 0.001
    combos = list(product(spacings, levels_options, stop_loss_pcts))
    results: list[SweepResult] = []

    for module in _NOISY_MODULES:
        logger.disable(module)
    try:
        for done, (spacing, levels, stop_loss) in enumerate(combos):
            if progress is not None:
                progress(done, len(combos))
            if spacing < fee_floor:
                results.append(
                    SweepResult(
                        spacing_pct=spacing, levels=levels, stop_loss_pct=stop_loss,
                        viable=False,
                        reason=f"spacing below fee floor {fee_floor:.3%}",
                    )
                )
                continue

            async def run_window(window: Sequence[Candle]):
                settings = Settings(
                    _env_file=None,
                    symbols=[symbol],
                    strategy="grid",
                    grid_spacing_pct=spacing,
                    grid_levels=levels,
                    stop_loss_pct=stop_loss,
                    max_open_orders=max(10, levels + 2),
                )
                strategy = GridTradingStrategy(
                    symbol,
                    levels=levels,
                    spacing_pct=spacing,
                    order_quote_size=order_quote_size,
                    aligned_protection=aligned_protection,
                    stop_loss_buffer_pct=stop_loss,
                    # Fresh (stateful) filter per run so windows stay independent.
                    regime_filter=RegimeFilter() if regime_filter else None,
                )
                engine = BacktestEngine(
                    strategy=strategy,
                    settings=settings,
                    initial_capital=initial_capital,
                    maker_fee_rate=maker_fee_rate,
                    taker_fee_rate=taker_fee_rate,
                    slippage_rate=slippage_rate,
                )
                return await engine.run(list(window), timeframe=timeframe)

            full = await run_window(candles)
            train = await run_window(candles[:split])
            test = await run_window(candles[split:])
            metrics = compute_metrics(full)

            results.append(
                SweepResult(
                    spacing_pct=spacing,
                    levels=levels,
                    stop_loss_pct=stop_loss,
                    viable=True,
                    reason=None,
                    full_return_pct=metrics.total_return_pct,
                    train_return_pct=100.0 * (train.final_equity / initial_capital - 1.0),
                    test_return_pct=100.0 * (test.final_equity / initial_capital - 1.0),
                    benchmark_return_pct=metrics.benchmark_return_pct,
                    max_drawdown_pct=metrics.max_drawdown_pct,
                    sharpe_ratio=metrics.sharpe_ratio,
                    win_rate_pct=metrics.win_rate_pct,
                    profit_factor=metrics.profit_factor,
                    total_trades=metrics.total_trades,
                    total_fees=metrics.total_fees,
                )
            )
    finally:
        for module in _NOISY_MODULES:
            logger.enable(module)

    if progress is not None:
        progress(len(combos), len(combos))
    results.sort(key=lambda r: (not r.robust, not r.viable, -r.test_return_pct))
    return results
