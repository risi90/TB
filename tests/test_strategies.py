"""Unit tests for strategy signal generation."""

from __future__ import annotations

import pytest

from engine.candle_aggregator import Bar
from engine.models import Order, OrderSide, OrderStatus, OrderType
from strategies.grid_trading import GridTradingStrategy, _LevelState
from strategies.sma_crossover import SmaCrossoverStrategy
from tests.conftest import make_ticker


# ---------------------------------------------------------------------------
# Grid trading
# ---------------------------------------------------------------------------
async def test_grid_builds_levels_and_emits_buy_limits() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=3, spacing_pct=0.01, order_quote_size=100.0
    )
    await strategy.on_ticker(make_ticker(bid=99.0, ask=101.0))  # mid = 100
    signals = strategy.generate_signals()

    assert len(signals) == 3
    assert all(s.side is OrderSide.BUY and s.type is OrderType.LIMIT for s in signals)
    assert [s.price for s in signals] == pytest.approx([99.0, 98.0, 97.0])
    # Each order is worth ~100 EUR of quote.
    for signal in signals:
        assert signal.amount * signal.price == pytest.approx(100.0)  # type: ignore[operator]
    # Signals are emitted exactly once.
    assert strategy.generate_signals() == []


async def test_grid_buy_fill_queues_matching_sell() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0
    )
    await strategy.on_ticker(make_ticker(bid=99.0, ask=101.0))
    buy_requests = strategy.generate_signals()

    # Simulate the router accepting the first buy and it filling.
    filled = Order(
        symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
        amount=buy_requests[0].amount, price=buy_requests[0].price,
        status=OrderStatus.FILLED, filled=buy_requests[0].amount,
    )
    strategy.register_order(buy_requests[0], filled)
    await strategy.on_fill(filled)

    sells = strategy.generate_signals()
    assert len(sells) == 1
    assert sells[0].side is OrderSide.SELL
    assert sells[0].type is OrderType.LIMIT
    assert sells[0].price == pytest.approx(100.0)  # one level above the 99.0 buy
    assert sells[0].amount == pytest.approx(filled.filled)
    assert sells[0].reduce_only


async def test_grid_level_rearms_after_sell_fill() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=1, spacing_pct=0.01, order_quote_size=100.0
    )
    await strategy.on_ticker(make_ticker(bid=99.0, ask=101.0))
    [buy_request] = strategy.generate_signals()
    buy = Order(
        symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
        amount=buy_request.amount, price=buy_request.price,
        status=OrderStatus.FILLED, filled=buy_request.amount,
    )
    strategy.register_order(buy_request, buy)
    await strategy.on_fill(buy)

    [sell_request] = strategy.generate_signals()
    sell = Order(
        symbol="BTC/EUR", side=OrderSide.SELL, type=OrderType.LIMIT,
        amount=sell_request.amount, price=sell_request.price,
        status=OrderStatus.FILLED, filled=sell_request.amount,
    )
    strategy.register_order(sell_request, sell)
    await strategy.on_fill(sell)

    assert strategy.grid[0].state is _LevelState.IDLE
    # The next ticker re-arms the level with a fresh buy.
    await strategy.on_ticker(make_ticker(bid=99.0, ask=101.0))
    rearmed = strategy.generate_signals()
    assert len(rearmed) == 1
    assert rearmed[0].side is OrderSide.BUY


# ---------------------------------------------------------------------------
# SMA crossover (bar-driven: indicators only move on completed bar closes)
# ---------------------------------------------------------------------------
async def _feed(strategy: SmaCrossoverStrategy, closes: list[float]) -> None:
    for i, close in enumerate(closes):
        await strategy.on_bar_close(
            Bar(timestamp=i * 60.0, open=close, high=close, low=close, close=close)
        )


async def test_sma_golden_cross_emits_buy() -> None:
    strategy = SmaCrossoverStrategy(
        "BTC/EUR", fast_period=2, slow_period=4, order_quote_size=100.0
    )
    # Downtrend to fill windows with fast below slow, then a sharp rally.
    await _feed(strategy, [110.0, 108.0, 106.0, 104.0, 102.0])
    assert strategy.generate_signals() == []
    await _feed(strategy, [112.0, 120.0])
    signals = strategy.generate_signals()
    assert len(signals) == 1
    assert signals[0].side is OrderSide.BUY
    assert signals[0].type is OrderType.MARKET


async def test_sma_raw_ticks_never_move_the_indicator() -> None:
    strategy = SmaCrossoverStrategy(
        "BTC/EUR", fast_period=2, slow_period=4, order_quote_size=100.0
    )
    for price in [110.0, 108.0, 106.0, 104.0, 102.0, 112.0, 120.0]:
        await strategy.on_ticker(make_ticker(bid=price - 0.5, ask=price + 0.5, last=price))
    assert strategy.fast_sma is None
    assert strategy.generate_signals() == []


async def test_sma_death_cross_emits_sell_only_with_position() -> None:
    strategy = SmaCrossoverStrategy(
        "BTC/EUR", fast_period=2, slow_period=4, order_quote_size=100.0
    )
    await _feed(strategy, [110.0, 108.0, 106.0, 104.0, 102.0, 112.0, 120.0])
    [buy] = strategy.generate_signals()

    # Simulate the buy filling so the strategy holds a position.
    await strategy.on_fill(
        Order(
            symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET,
            amount=buy.amount, status=OrderStatus.FILLED, filled=buy.amount,
        )
    )
    # Sharp sell-off crosses fast back under slow.
    await _feed(strategy, [100.0, 90.0])
    signals = strategy.generate_signals()
    assert len(signals) == 1
    assert signals[0].side is OrderSide.SELL
    assert signals[0].amount == pytest.approx(buy.amount)
    assert signals[0].reduce_only


async def test_sma_no_sell_without_position() -> None:
    strategy = SmaCrossoverStrategy(
        "BTC/EUR", fast_period=2, slow_period=4, order_quote_size=100.0
    )
    # Uptrend then crash — but no fill was ever reported.
    await _feed(strategy, [100.0, 102.0, 104.0, 106.0, 108.0, 90.0, 80.0])
    signals = strategy.generate_signals()
    assert all(s.side is not OrderSide.SELL for s in signals)


def test_sma_rejects_bad_periods() -> None:
    with pytest.raises(ValueError):
        SmaCrossoverStrategy("BTC/EUR", fast_period=10, slow_period=10)
