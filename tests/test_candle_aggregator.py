"""Tests for tick-to-bar aggregation and bar-close strategy triggers."""

from __future__ import annotations

import pytest

from engine.candle_aggregator import Bar, CandleAggregator, timeframe_to_seconds
from strategies.sma_crossover import SmaCrossoverStrategy
from tests.conftest import make_ticker


def test_ohlcv_bucketing() -> None:
    agg = CandleAggregator("1m")
    assert agg.add_tick(100.0, 0.0) is None
    assert agg.add_tick(105.0, 10.0) is None
    assert agg.add_tick(95.0, 20.0, volume=2.0) is None
    assert agg.add_tick(102.0, 59.9, volume=1.0) is None

    completed = agg.add_tick(103.0, 60.0)  # first tick of the next bucket
    assert completed == Bar(
        timestamp=0.0, open=100.0, high=105.0, low=95.0, close=102.0, volume=3.0
    )
    # The new in-progress bar opened with the boundary tick.
    assert agg.current_bar == Bar(60.0, 103.0, 103.0, 103.0, 103.0, 0.0)


def test_first_tick_timestamp_is_bucket_aligned() -> None:
    agg = CandleAggregator("1m")
    agg.add_tick(50.0, 75.0)  # 75s -> bucket [60, 120)
    assert agg.current_bar is not None
    assert agg.current_bar.timestamp == 60.0


def test_gap_spanning_multiple_buckets_emits_single_bar() -> None:
    agg = CandleAggregator("1m")
    agg.add_tick(100.0, 0.0)
    completed = agg.add_tick(101.0, 250.0)  # skips buckets 60 and 120
    assert completed is not None
    assert completed.timestamp == 0.0  # only the old bucket's bar, no synthetics
    assert agg.current_bar is not None
    assert agg.current_bar.timestamp == 240.0


def test_five_minute_buckets() -> None:
    agg = CandleAggregator("5m")
    assert timeframe_to_seconds("5m") == 300.0
    agg.add_tick(10.0, 0.0)
    assert agg.add_tick(11.0, 299.0) is None  # still inside the 5m bucket
    completed = agg.add_tick(12.0, 300.0)
    assert completed is not None
    assert completed.close == 11.0


def test_force_close_returns_in_progress_bar() -> None:
    agg = CandleAggregator("1m")
    assert agg.force_close() is None  # nothing yet
    agg.add_tick(100.0, 0.0)
    bar = agg.force_close()
    assert bar is not None and bar.open == bar.close == 100.0
    assert agg.current_bar is None


def test_invalid_timeframe_rejected() -> None:
    with pytest.raises(ValueError):
        CandleAggregator("3w")


# ---------------------------------------------------------------------------
# Tick-rate distortion removal
# ---------------------------------------------------------------------------
async def test_sma_ignores_ticks_and_reacts_only_to_bars() -> None:
    """A tick burst must not move the SMA — only completed bars count."""
    strategy = SmaCrossoverStrategy("BTC/EUR", fast_period=2, slow_period=3)

    # A storm of ticks (as a fast market would produce) changes nothing.
    for i in range(500):
        await strategy.on_ticker(make_ticker(bid=99.0 + i, ask=101.0 + i))
    assert strategy.fast_sma is None
    assert strategy.generate_signals() == []

    # Bars drive the indicator: downtrend, then a rally -> golden cross.
    for i, close in enumerate([110.0, 105.0, 100.0, 95.0]):
        await strategy.on_bar_close(Bar(i * 60.0, close, close, close, close))
    assert strategy.generate_signals() == []
    for i, close in enumerate([120.0, 130.0], start=4):
        await strategy.on_bar_close(Bar(i * 60.0, close, close, close, close))
    signals = strategy.generate_signals()
    assert len(signals) == 1
    assert signals[0].side.value == "buy"


async def test_aggregator_bar_feeds_strategy_like_backtest() -> None:
    """Ticks -> aggregator -> on_bar_close equals feeding closes directly."""
    via_aggregator = SmaCrossoverStrategy("BTC/EUR", fast_period=2, slow_period=3)
    direct = SmaCrossoverStrategy("BTC/EUR", fast_period=2, slow_period=3)
    agg = CandleAggregator("1m")

    closes = [110.0, 105.0, 100.0, 112.0, 125.0]
    # Each bar: three intra-minute ticks, close = last tick of the minute.
    ts = 0.0
    for close in closes:
        for price in (close + 1.0, close - 1.0, close):
            bar = agg.add_tick(price, ts)
            if bar is not None:
                await via_aggregator.on_bar_close(bar)
            ts += 20.0
    for i, close in enumerate(closes[:-1]):  # last bar still in progress
        await direct.on_bar_close(Bar(i * 60.0, close, close, close, close))

    assert via_aggregator.fast_sma == direct.fast_sma
    assert via_aggregator.slow_sma == direct.slow_sma
