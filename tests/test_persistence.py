"""Persistence tests: schema creation, state saving, and restart rehydration."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.models import (
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from engine.paper_engine import Balance, PaperEngine
from storage.db import Database
from storage.persistence import PersistenceManager
from strategies.grid_trading import GridTradingStrategy, _LevelState
from tests.conftest import make_ticker


@pytest.fixture()
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.db")
    await database.connect()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# Schema & primitives
# ---------------------------------------------------------------------------
async def test_schema_created(db: Database) -> None:
    async with db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cursor:
        tables = {row["name"] for row in await cursor.fetchall()}
    assert {
        "bot_config", "positions", "orders", "grid_state",
        "account_balances", "equity_history",
    } <= tables


async def test_config_roundtrip(db: Database) -> None:
    await db.set_config("bot_status", "paused")
    assert await db.get_config("bot_status") == "paused"
    await db.set_config("bot_status", "running")  # upsert overwrites
    await db.set_config_many({"a": "1", "b": "2"})
    all_config = await db.get_all_config()
    assert all_config["bot_status"] == "running"
    assert all_config["a"] == "1" and all_config["b"] == "2"
    assert await db.get_config("missing") is None


async def test_seed_config_does_not_overwrite(db: Database) -> None:
    await db.set_config("grid_levels", "8")
    await db.seed_config({"grid_levels": "5", "fresh_key": "x"})
    assert await db.get_config("grid_levels") == "8"
    assert await db.get_config("fresh_key") == "x"


async def test_balances_roundtrip(db: Database) -> None:
    await db.save_balances(
        {
            "EUR": Balance(total=10_000.0, available=9_500.0),
            "BTC": Balance(total=0.5, available=0.5),
        }
    )
    loaded = await db.load_balances()
    assert loaded["EUR"].total == pytest.approx(10_000.0)
    assert loaded["EUR"].available == pytest.approx(9_500.0)  # reserved = 500
    assert loaded["BTC"].available == pytest.approx(0.5)


async def test_position_roundtrip(db: Database) -> None:
    position = Position(
        symbol="BTC/EUR", amount=0.25, entry_price=40_000.0,
        realized_pnl=12.5, stop_loss=39_200.0, take_profit=41_600.0,
    )
    await db.upsert_position(position)
    loaded = await db.load_positions()
    restored = loaded["BTC/EUR"]
    assert restored.amount == pytest.approx(0.25)
    assert restored.entry_price == pytest.approx(40_000.0)
    assert restored.realized_pnl == pytest.approx(12.5)
    assert restored.stop_loss == pytest.approx(39_200.0)
    assert restored.take_profit == pytest.approx(41_600.0)


async def test_order_roundtrip_and_open_filter(db: Database) -> None:
    open_order = Order(
        symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
        amount=0.1, price=39_000.0, stop_loss=38_220.0, take_profit=40_560.0,
    )
    filled_order = Order(
        symbol="BTC/EUR", side=OrderSide.SELL, type=OrderType.MARKET,
        amount=0.05, status=OrderStatus.FILLED, filled=0.05,
        average_price=40_100.0, fee_paid=2.0, reduce_only=True,
    )
    await db.upsert_order(open_order, strategy_id="grid:BTC/EUR")
    await db.upsert_order(filled_order)

    open_rows = await db.load_open_orders()
    assert len(open_rows) == 1
    restored, strategy_id = open_rows[0]
    assert restored.id == open_order.id
    assert restored.price == pytest.approx(39_000.0)
    assert restored.stop_loss == pytest.approx(38_220.0)
    assert strategy_id == "grid:BTC/EUR"

    recent = await db.load_recent_orders()
    assert {o.id for o in recent} == {open_order.id, filled_order.id}


async def test_equity_history_roundtrip(db: Database) -> None:
    await db.record_equity(10_000.0, 0.0, 0.0, timestamp=100.0)
    await db.record_equity(10_050.0, 25.0, 25.0, timestamp=200.0)
    history = await db.load_equity_history()
    assert len(history) == 2
    assert history[0][0] == pytest.approx(100.0)  # oldest first
    assert history[-1][1] == pytest.approx(10_050.0)


async def test_grid_state_roundtrip(db: Database) -> None:
    await db.save_grid_state("grid:BTC/EUR", "BTC/EUR", '{"levels": 3}', "[]")
    row = await db.load_grid_state("grid:BTC/EUR")
    assert row is not None
    assert row.symbol == "BTC/EUR"
    assert row.grid_config_json == '{"levels": 3}'
    await db.delete_grid_state("grid:BTC/EUR")
    assert await db.load_grid_state("grid:BTC/EUR") is None


# ---------------------------------------------------------------------------
# Engine rehydration after a simulated restart
# ---------------------------------------------------------------------------
async def test_engine_rehydration_after_restart(db: Database) -> None:
    persistence = PersistenceManager(db)

    # --- first "process": trade a bit, then persist ---------------------
    engine1 = PaperEngine.create({"EUR": 10_000.0})
    engine1.process_ticker(make_ticker(bid=99.0, ask=100.0, last=100.0))
    engine1.create_order(
        OrderRequest(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=1.0)
    )
    resting, _ = engine1.create_order(
        OrderRequest(
            symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
            amount=0.5, price=90.0,
        )
    )
    await persistence.persist_account(engine1)
    await persistence.persist_orders(
        list(engine1.open_orders.values()), {resting.id: "grid:BTC/EUR"}
    )

    # --- second "process": fresh engine, hydrate from SQLite ------------
    engine2 = PaperEngine.create({"EUR": 10_000.0})
    strategy_map = await persistence.hydrate_engine(engine2)

    assert strategy_map == {resting.id: "grid:BTC/EUR"}
    assert engine2.balances["EUR"].total == pytest.approx(engine1.balances["EUR"].total)
    assert engine2.balances["EUR"].available == pytest.approx(
        engine1.balances["EUR"].available
    )
    assert engine2.balances["BTC"].total == pytest.approx(1.0)
    assert engine2.positions["BTC/EUR"].amount == pytest.approx(1.0)
    assert engine2.positions["BTC/EUR"].entry_price == pytest.approx(100.0)
    assert set(engine2.open_orders) == {resting.id}

    # The restored order still matches and settles like a live one.
    assert engine2.process_ticker(make_ticker(bid=94.0, ask=95.0)) == []
    fills = engine2.process_ticker(make_ticker(bid=88.0, ask=89.0))
    assert len(fills) == 1
    assert fills[0].order_id == resting.id
    assert fills[0].price == pytest.approx(90.0)
    assert engine2.positions["BTC/EUR"].amount == pytest.approx(1.5)


async def test_restored_order_cancel_releases_reservation(db: Database) -> None:
    persistence = PersistenceManager(db)
    engine1 = PaperEngine.create({"EUR": 10_000.0})
    engine1.process_ticker(make_ticker(bid=99.0, ask=100.0))
    resting, _ = engine1.create_order(
        OrderRequest(
            symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
            amount=1.0, price=95.0,
        )
    )
    await persistence.persist_account(engine1)
    await persistence.persist_order(resting)

    engine2 = PaperEngine.create({"EUR": 10_000.0})
    await persistence.hydrate_engine(engine2)
    assert engine2.balances["EUR"].available < 10_000.0
    engine2.cancel_order(resting.id)
    assert engine2.balances["EUR"].available == pytest.approx(10_000.0)


async def test_hydrate_fresh_db_keeps_configured_balances(db: Database) -> None:
    persistence = PersistenceManager(db)
    engine = PaperEngine.create({"EUR": 1_234.0})
    assert await persistence.hydrate_engine(engine) == {}
    assert engine.balances["EUR"].total == pytest.approx(1_234.0)


# ---------------------------------------------------------------------------
# Grid strategy state rehydration
# ---------------------------------------------------------------------------
async def test_grid_strategy_state_survives_restart() -> None:
    strategy1 = GridTradingStrategy(
        "BTC/EUR", levels=3, spacing_pct=0.01, order_quote_size=100.0
    )
    await strategy1.on_ticker(make_ticker(bid=99.0, ask=101.0))  # anchor at 100
    [buy1, *_] = strategy1.generate_signals()
    order = Order(
        symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
        amount=buy1.amount, price=buy1.price,
    )
    strategy1.register_order(buy1, order)

    config, levels = strategy1.to_state()

    # "Restart": a fresh instance with the same parameters restores the state.
    strategy2 = GridTradingStrategy(
        "BTC/EUR", levels=3, spacing_pct=0.01, order_quote_size=100.0
    )
    assert strategy2.matches_config(config)
    strategy2.restore_state(config, levels, open_order_ids={order.id})

    assert [lvl.buy_price for lvl in strategy2.grid] == pytest.approx(
        [lvl.buy_price for lvl in strategy1.grid]
    )
    assert strategy2.grid[0].state is _LevelState.BUY_PENDING
    assert strategy2.grid[0].buy_order_id == order.id
    # The restored strategy reacts to the tracked order's fill as before.
    order.status = OrderStatus.FILLED
    order.filled = order.amount
    await strategy2.on_fill(order)
    [sell] = strategy2.generate_signals()
    assert sell.side is OrderSide.SELL
    assert sell.price == pytest.approx(strategy2.grid[0].sell_price)


async def test_grid_restore_repairs_lost_orders() -> None:
    strategy1 = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0
    )
    await strategy1.on_ticker(make_ticker(bid=99.0, ask=101.0))
    requests = strategy1.generate_signals()
    orders = [
        Order(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
              amount=r.amount, price=r.price)
        for r in requests
    ]
    for request, order in zip(requests, orders):
        strategy1.register_order(request, order)
    config, levels = strategy1.to_state()

    strategy2 = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0
    )
    # Only the first order still exists in the engine after "restart".
    strategy2.restore_state(config, levels, open_order_ids={orders[0].id})
    assert strategy2.grid[0].state is _LevelState.BUY_PENDING
    assert strategy2.grid[1].state is _LevelState.IDLE  # repaired
    assert strategy2.grid[1].buy_order_id is None

    # A changed grid configuration is detected so stale state is discarded.
    changed = GridTradingStrategy(
        "BTC/EUR", levels=5, spacing_pct=0.01, order_quote_size=100.0
    )
    assert not changed.matches_config(config)
