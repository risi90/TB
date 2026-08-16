"""Tests for the efficiency-ratio regime filter and its grid integration."""

from __future__ import annotations

import math

import pytest

from config.config import Settings
from backtester.engine import BacktestEngine
from backtester.models import Candle
from engine.candle_aggregator import Bar
from engine.models import Order, OrderSide, OrderType
from strategies.grid_trading import GridTradingStrategy, _LevelState
from strategies.regime import Regime, RegimeFilter
from tests.conftest import make_ticker


def _bar(i: int, close: float) -> Bar:
    return Bar(timestamp=i * 3600.0, open=close, high=close, low=close, close=close)


# ---------------------------------------------------------------------------
# RegimeFilter unit behavior
# ---------------------------------------------------------------------------
def test_stays_ranging_until_window_full() -> None:
    detector = RegimeFilter(window=10)
    for i in range(9):
        assert detector.update(100.0 + i) is Regime.RANGING
    assert not detector.warmed_up


def test_clean_trends_are_detected_with_direction() -> None:
    up = RegimeFilter(window=10)
    for i in range(20):
        regime = up.update(100.0 + i)  # straight line up: ER = 1.0
    assert regime is Regime.TRENDING_UP
    assert up.efficiency_ratio == pytest.approx(1.0)

    down = RegimeFilter(window=10)
    for i in range(20):
        regime = down.update(100.0 - i)
    assert regime is Regime.TRENDING_DOWN


def test_choppy_market_reads_as_ranging() -> None:
    detector = RegimeFilter(window=10)
    for i in range(40):
        regime = detector.update(100.0 + 3.0 * math.sin(i))  # pure oscillation
        assert regime is Regime.RANGING  # never flips to trending, ever
    assert detector.efficiency_ratio < detector._enter  # noqa: SLF001


def test_hysteresis_keeps_regime_between_thresholds() -> None:
    detector = RegimeFilter(window=4, enter_threshold=0.6, exit_threshold=0.3)
    # Clean drop -> trending down (ER = 1.0).
    for close in [100.0, 98.0, 96.0, 94.0, 92.0]:
        detector.update(close)
    assert detector.regime is Regime.TRENDING_DOWN
    detector.update(93.0)  # ER ~0.71, still above enter -> down
    assert detector.regime is Regime.TRENDING_DOWN
    detector.update(94.0)  # ER ~0.33: inside the (0.3, 0.6) band -> held
    assert 0.3 < detector.efficiency_ratio < 0.6
    assert detector.regime is Regime.TRENDING_DOWN
    detector.update(95.0)  # ER 0.2, below exit -> back to ranging
    assert detector.regime is Regime.RANGING


def test_invalid_parameters_rejected() -> None:
    with pytest.raises(ValueError):
        RegimeFilter(window=1)
    with pytest.raises(ValueError):
        RegimeFilter(enter_threshold=0.2, exit_threshold=0.3)  # inverted


# ---------------------------------------------------------------------------
# Grid integration
# ---------------------------------------------------------------------------
async def test_downtrend_suspends_entries_and_cancels_resting_buys() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        auto_reanchor=False,
        regime_filter=RegimeFilter(window=5, enter_threshold=0.6, exit_threshold=0.3),
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))  # anchor 100
    buys = strategy.generate_signals()
    orders = [
        Order(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
              amount=r.amount, price=r.price)
        for r in buys
    ]
    for request, order in zip(buys, orders):
        strategy.register_order(request, order)

    # Feed a clean downtrend through the bar hook.
    for i, close in enumerate([100.0, 98.0, 96.0, 94.0, 92.0, 90.0]):
        await strategy.on_bar_close(_bar(i, close))

    assert not strategy.entries_allowed
    assert set(strategy.generate_cancellations()) == {o.id for o in orders}
    assert all(lvl.state is _LevelState.IDLE for lvl in strategy.grid)
    # New ticks arm nothing while the downtrend persists.
    await strategy.on_ticker(make_ticker(bid=89.5, ask=90.5))
    assert strategy.generate_signals() == []


async def test_entries_resume_when_market_ranges_again() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        auto_reanchor=False,
        regime_filter=RegimeFilter(window=5, enter_threshold=0.6, exit_threshold=0.3),
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))
    strategy.generate_signals()

    for i, close in enumerate([100.0, 98.0, 96.0, 94.0, 92.0, 90.0]):
        await strategy.on_bar_close(_bar(i, close))
    assert not strategy.entries_allowed

    # Sideways chop brings the efficiency ratio down -> ranging again.
    for i, close in enumerate([91.0, 90.0, 91.0, 90.0, 91.0, 90.0], start=6):
        await strategy.on_bar_close(_bar(i, close))
    assert strategy.entries_allowed
    await strategy.on_ticker(make_ticker(bid=89.5, ask=90.5))
    assert len(strategy.generate_signals()) == 2  # re-armed


async def test_filter_disabled_keeps_old_behavior() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        regime_filter=None,
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))
    strategy.generate_signals()
    for i, close in enumerate([100.0, 90.0, 80.0, 70.0, 60.0, 50.0]):
        await strategy.on_bar_close(_bar(i, close))
    assert strategy.entries_allowed  # no filter, no gating


async def test_backtest_filter_blocks_buys_during_crash() -> None:
    """In a sustained crash the filtered grid stops buying; unfiltered keeps
    catching knives. The filtered variant must end with fewer buys."""
    crash = [Candle(timestamp=i * 3600.0, open=p, high=p + 0.5, low=p - 0.5, close=p)
             for i, p in enumerate(100.0 - 0.8 * i for i in range(60))]
    settings = Settings(_env_file=None, stop_loss_pct=0.15, take_profit_pct=0.2)

    async def run(filtered: bool) -> int:
        strategy = GridTradingStrategy(
            "BTC/EUR", levels=3, spacing_pct=0.01, order_quote_size=100.0,
            stop_loss_buffer_pct=0.15,
            regime_filter=(
                RegimeFilter(window=8, enter_threshold=0.5, exit_threshold=0.3)
                if filtered else None
            ),
        )
        engine = BacktestEngine(strategy, settings, initial_capital=10_000.0,
                                slippage_rate=0.0)
        result = await engine.run(crash, timeframe="1h")
        return sum(1 for t in result.trades if t.side == "buy")

    buys_filtered = await run(filtered=True)
    buys_unfiltered = await run(filtered=False)
    assert buys_filtered < buys_unfiltered
