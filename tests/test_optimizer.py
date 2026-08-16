"""Tests for grid-aligned protection and the parameter sweep."""

from __future__ import annotations

import math

import pytest

from config.config import Settings
from backtester.engine import BacktestEngine
from backtester.models import Candle
from backtester.optimizer import SweepResult, sweep_grid
from strategies.grid_trading import GridTradingStrategy
from tests.conftest import make_ticker

_T0 = 1_700_000_000.0


def _candle(i: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(timestamp=_T0 + i * 3600, open=o, high=h, low=low, close=c)


def _wave_candles(n: int, base: float = 100.0, amplitude: float = 3.0) -> list[Candle]:
    """Sideways oscillating market — the regime a grid should profit in."""
    candles = []
    for i in range(n):
        mid = base + amplitude * math.sin(i / 3.0)
        candles.append(_candle(i, mid, mid + 1.0, mid - 1.0, mid))
    return candles


# ---------------------------------------------------------------------------
# Grid-aligned protective levels
# ---------------------------------------------------------------------------
async def test_aligned_protection_stamps_grid_floor_levels() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        aligned_protection=True, stop_loss_buffer_pct=0.02,
        take_profit_buffer_pct=0.05,
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))  # anchor 100
    signals = strategy.generate_signals()
    floor = 100.0 * (1 - 0.01 * 2)  # lowest rung: 98
    assert strategy.grid_floor_price() == pytest.approx(floor)
    for signal in signals:
        assert signal.stop_loss == pytest.approx(floor * 0.98)  # below the GRID
        assert signal.take_profit == pytest.approx(100.0 * 1.05)  # above anchor


async def test_disabled_alignment_leaves_levels_unset() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        aligned_protection=False,
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))
    for signal in strategy.generate_signals():
        assert signal.stop_loss is None  # risk manager stamps per-entry defaults
        assert signal.take_profit is None


async def test_grid_survives_dip_that_kills_per_entry_stops() -> None:
    """A dip below the per-entry stop level but above the grid floor must NOT
    trigger a protective exit — the grid completes its cycles instead."""
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        aligned_protection=True, stop_loss_buffer_pct=0.02,
        take_profit_buffer_pct=0.10,
    )
    settings = Settings(_env_file=None, stop_loss_pct=0.02, take_profit_pct=0.1)
    engine = BacktestEngine(
        strategy, settings, initial_capital=10_000.0, slippage_rate=0.0
    )
    candles = [
        _candle(0, 100.0, 100.0, 100.0, 100.0),  # anchor; rungs 99 / 98, SL 96.04
        _candle(1, 100.0, 100.0, 96.5, 97.0),    # both rungs fill; dip to 96.5
        _candle(2, 97.0, 100.5, 97.0, 100.0),    # recovery: both sells fill
    ]
    result = await engine.run(candles, timeframe="1h")

    sells = [t for t in result.trades if t.side == "sell"]
    assert all(t.type == "limit" for t in sells)  # grid sells, no market SL exit
    assert strategy.total_cycles == 2
    assert sum(t.realized_pnl for t in sells) > 0


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------
async def test_sweep_covers_all_combinations_and_sorts_robust_first() -> None:
    candles = _wave_candles(120)
    progress_calls: list[tuple[int, int]] = []
    results = await sweep_grid(
        candles, "BTC/EUR", "1h",
        spacings=[0.01, 0.02],
        levels_options=[2],
        stop_loss_pcts=[0.1],
        order_quote_size=100.0,
        progress=lambda done, total: progress_calls.append((done, total)),
    )
    assert len(results) == 2
    assert all(isinstance(r, SweepResult) and r.viable for r in results)
    assert all(r.total_trades > 0 for r in results)  # waves produce cycles
    assert progress_calls[-1] == (2, 2)
    # Robust results (if any) are sorted before non-robust ones.
    flags = [r.robust for r in results]
    assert flags == sorted(flags, reverse=True)


async def test_sweep_excludes_combinations_below_fee_floor() -> None:
    candles = _wave_candles(60)
    results = await sweep_grid(
        candles, "BTC/EUR", "1h",
        spacings=[0.001, 0.02],  # 0.1% is below the 0.4% fee floor
        levels_options=[2],
        stop_loss_pcts=[0.1],
        maker_fee_rate=0.0015,
    )
    excluded = [r for r in results if not r.viable]
    assert len(excluded) == 1
    assert excluded[0].spacing_pct == pytest.approx(0.001)
    assert "fee floor" in (excluded[0].reason or "")
    assert excluded[0].total_trades == 0  # never backtested


async def test_sweep_is_deterministic() -> None:
    candles = _wave_candles(80)
    kwargs = dict(
        spacings=[0.02], levels_options=[2], stop_loss_pcts=[0.1],
        order_quote_size=100.0,
    )
    first = await sweep_grid(candles, "BTC/EUR", "1h", **kwargs)
    second = await sweep_grid(candles, "BTC/EUR", "1h", **kwargs)
    assert [r.to_dict() for r in first] == [r.to_dict() for r in second]


async def test_sweep_rejects_tiny_series() -> None:
    with pytest.raises(ValueError):
        await sweep_grid(
            _wave_candles(5), "BTC/EUR", "1h",
            spacings=[0.02], levels_options=[2], stop_loss_pcts=[0.1],
        )
