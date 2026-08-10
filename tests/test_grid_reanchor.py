"""Tests for grid re-anchoring and the committed-inventory cap."""

from __future__ import annotations

import pytest

from engine.models import Order, OrderRequest, OrderSide, OrderStatus, OrderType
from strategies.grid_trading import GridTradingStrategy, _LevelState
from tests.conftest import make_ticker


def _order_for(request: OrderRequest) -> Order:
    return Order(
        symbol=request.symbol, side=request.side, type=request.type,
        amount=request.amount, price=request.price,
    )


async def _fill(strategy: GridTradingStrategy, request: OrderRequest, order: Order) -> None:
    order.status = OrderStatus.FILLED
    order.filled = order.amount
    await strategy.on_fill(order)


async def test_reanchor_triggers_beyond_drift_threshold() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        auto_reanchor=True, reanchor_factor=1.5,
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))  # anchor 100
    buys = strategy.generate_signals()
    orders = [_order_for(r) for r in buys]
    for request, order in zip(buys, orders):
        strategy.register_order(request, order)

    # Grid width = 100 * 0.01 * 2 = 2 -> threshold = 3. Drift of 2 is fine:
    await strategy.on_ticker(make_ticker(bid=101.5, ask=102.5))
    assert strategy.generate_cancellations() == []
    assert strategy.reanchor_count == 0

    # Drift of 4 exceeds the threshold -> re-anchor at 104.
    await strategy.on_ticker(make_ticker(bid=103.5, ask=104.5))
    assert strategy.reanchor_count == 1
    assert set(strategy.generate_cancellations()) == {o.id for o in orders}
    new_buys = strategy.generate_signals()
    assert [s.price for s in new_buys] == pytest.approx([104 * 0.99, 104 * 0.98])
    # Cancellations drain: not emitted twice.
    assert strategy.generate_cancellations() == []


async def test_no_reanchor_when_disabled() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        auto_reanchor=False,
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))
    strategy.generate_signals()
    await strategy.on_ticker(make_ticker(bid=199.5, ask=200.5))  # huge drift
    assert strategy.reanchor_count == 0
    assert strategy.generate_cancellations() == []


async def test_downward_reanchor_keeps_inventory_and_respects_cap() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        auto_reanchor=True, reanchor_factor=1.5,  # cap defaults to 200 (2x100)
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))  # anchor 100
    buys = strategy.generate_signals()
    orders = [_order_for(r) for r in buys]
    for request, order in zip(buys, orders):
        strategy.register_order(request, order)
        await _fill(strategy, request, order)  # both rungs bought on the way down

    sells = strategy.generate_signals()
    sell_orders = [_order_for(r) for r in sells]
    for request, order in zip(sells, sell_orders):
        strategy.register_order(request, order)

    # Price collapses to 90 -> re-anchor. Inventory levels are kept (their
    # sells stay resting); the committed capital (~200) already fills the
    # cap, so NO new buys are armed below 90.
    await strategy.on_ticker(make_ticker(bid=89.5, ask=90.5))
    assert strategy.reanchor_count == 1
    assert strategy.generate_cancellations() == []  # no resting buys existed
    assert strategy.generate_signals() == []
    kept = [lvl for lvl in strategy.grid if lvl.state is _LevelState.SELL_PENDING]
    assert len(kept) == 2 and all(lvl.stale for lvl in kept)
    assert len(strategy.grid) == 4  # 2 fresh idle + 2 kept inventory levels

    # Selling one kept level frees capital: the level retires and one fresh
    # rung can arm on the next tick.
    await _fill(strategy, sells[0], sell_orders[0])
    assert len(strategy.grid) == 3  # stale level removed after its cycle
    await strategy.on_ticker(make_ticker(bid=89.5, ask=90.5))
    new_buys = strategy.generate_signals()
    assert len(new_buys) == 1
    assert new_buys[0].price == pytest.approx(90.0 * 0.99)


async def test_inventory_cap_limits_initial_arming() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=3, spacing_pct=0.01, order_quote_size=100.0,
        max_inventory_quote=200.0,
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))
    signals = strategy.generate_signals()
    assert len(signals) == 2  # third rung stays idle: 300 would exceed the cap
    armed = [lvl for lvl in strategy.grid if lvl.state is _LevelState.BUY_PENDING]
    assert len(armed) == 2


async def test_upward_reanchor_after_partial_fill_carries_inventory() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0,
        auto_reanchor=True, reanchor_factor=1.5, max_inventory_quote=1000.0,
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))
    buys = strategy.generate_signals()
    orders = [_order_for(r) for r in buys]
    for request, order in zip(buys, orders):
        strategy.register_order(request, order)
    # First rung fills; its sell is queued and registered.
    await _fill(strategy, buys[0], orders[0])
    [sell] = strategy.generate_signals()
    sell_order = _order_for(sell)
    strategy.register_order(sell, sell_order)

    # Rally to 106 -> re-anchor: the unfilled buy is canceled, the inventory
    # level is kept, and fresh rungs arm just below the new price.
    await strategy.on_ticker(make_ticker(bid=105.5, ask=106.5))
    assert strategy.reanchor_count == 1
    assert strategy.generate_cancellations() == [orders[1].id]
    new_buys = strategy.generate_signals()
    assert [s.price for s in new_buys] == pytest.approx([106 * 0.99, 106 * 0.98])
    kept = [lvl for lvl in strategy.grid if lvl.state is _LevelState.SELL_PENDING]
    assert len(kept) == 1 and kept[0].sell_order_id == sell_order.id


async def test_reanchor_state_survives_persistence_roundtrip() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0
    )
    await strategy.on_ticker(make_ticker(bid=99.5, ask=100.5))
    strategy.generate_signals()
    await strategy.on_ticker(make_ticker(bid=109.5, ask=110.5))  # re-anchor
    strategy.generate_cancellations()
    config, levels = strategy.to_state()
    assert config["reanchor_count"] == 1

    restored = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0
    )
    restored.restore_state(config, levels, open_order_ids=set())
    assert restored.reanchor_count == 1
    assert restored.grid[0].buy_price == pytest.approx(110 * 0.99)
