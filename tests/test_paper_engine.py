"""Unit tests for the paper trading engine: fills, fees, balances, PnL."""

from __future__ import annotations

import pytest

from engine.models import OrderRequest, OrderSide, OrderStatus, OrderType
from engine.paper_engine import InsufficientFunds, PaperEngine, UnknownMarketPrice
from tests.conftest import make_ticker


def test_market_buy_fills_at_ask_with_taker_fee(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=101.0))
    order, fills = engine.create_order(
        OrderRequest(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=1.0)
    )
    assert order.status is OrderStatus.FILLED
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(101.0)
    assert fills[0].fee == pytest.approx(101.0 * 0.0025)
    assert engine.balances["BTC"].total == pytest.approx(1.0)
    assert engine.balances["EUR"].total == pytest.approx(10_000.0 - 101.0 - 101.0 * 0.0025)


def test_market_order_without_ticker_is_rejected(engine: PaperEngine) -> None:
    with pytest.raises(UnknownMarketPrice):
        engine.create_order(
            OrderRequest(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=1.0)
        )
    assert engine.closed_orders[-1].status is OrderStatus.REJECTED


def test_resting_buy_limit_fills_when_ask_drops(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=101.0))
    order, fills = engine.create_order(
        OrderRequest(
            symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
            amount=1.0, price=95.0,
        )
    )
    assert fills == []
    assert order.is_open
    # Ask still above limit -> no fill.
    assert engine.process_ticker(make_ticker(bid=95.5, ask=96.0)) == []
    # Ask crosses the limit -> maker fill at the limit price.
    fills = engine.process_ticker(make_ticker(bid=94.0, ask=94.5))
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(95.0)
    assert fills[0].fee == pytest.approx(95.0 * 0.0015)
    assert order.status is OrderStatus.FILLED
    assert not engine.open_orders


def test_crossing_buy_limit_fills_immediately_as_taker(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=101.0))
    order, fills = engine.create_order(
        OrderRequest(
            symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
            amount=0.5, price=102.0,
        )
    )
    assert order.status is OrderStatus.FILLED
    assert fills[0].price == pytest.approx(101.0)  # fills at the ask, not the limit
    assert fills[0].fee == pytest.approx(0.5 * 101.0 * 0.0025)


def test_sell_limit_fills_when_bid_rises_and_realizes_net_pnl(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=100.0))
    engine.create_order(
        OrderRequest(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=1.0)
    )
    engine.create_order(
        OrderRequest(
            symbol="BTC/EUR", side=OrderSide.SELL, type=OrderType.LIMIT,
            amount=1.0, price=110.0,
        )
    )
    fills = engine.process_ticker(make_ticker(bid=111.0, ask=112.0))
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(110.0)
    position = engine.positions["BTC/EUR"]
    assert position.amount == 0.0
    # Realized PnL is NET of fees: gross 10 minus the entry fee (taker on the
    # market buy at 100) and the exit fee (maker on the limit sell at 110).
    entry_fee = 100.0 * 0.0025
    exit_fee = 110.0 * 0.0015
    assert position.realized_pnl == pytest.approx(10.0 - entry_fee - exit_fee)
    assert engine.realized_pnl() == pytest.approx(10.0 - entry_fee - exit_fee)


def test_partial_sell_allocates_entry_fees_proportionally(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=100.0))
    engine.create_order(
        OrderRequest(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=2.0)
    )
    position = engine.positions["BTC/EUR"]
    entry_fee_total = 2.0 * 100.0 * 0.0025
    assert position.entry_fees == pytest.approx(entry_fee_total)

    engine.process_ticker(make_ticker(bid=110.0, ask=111.0))
    engine.create_order(
        OrderRequest(symbol="BTC/EUR", side=OrderSide.SELL, type=OrderType.MARKET, amount=1.0)
    )
    # Half the position closed: half the entry fees are consumed.
    sell_fee = 110.0 * 0.0025
    assert position.realized_pnl == pytest.approx(10.0 - entry_fee_total / 2 - sell_fee)
    assert position.entry_fees == pytest.approx(entry_fee_total / 2)
    assert position.amount == pytest.approx(1.0)


def test_insufficient_quote_funds_rejected(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=101.0))
    with pytest.raises(InsufficientFunds):
        engine.create_order(
            OrderRequest(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=1000.0)
        )
    # Nothing was reserved or spent.
    assert engine.balances["EUR"].available == pytest.approx(10_000.0)
    assert not engine.open_orders


def test_cannot_sell_more_than_held(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=101.0))
    with pytest.raises(InsufficientFunds):
        engine.create_order(
            OrderRequest(symbol="BTC/EUR", side=OrderSide.SELL, type=OrderType.MARKET, amount=1.0)
        )


def test_reservation_prevents_double_spending(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=100.0))
    # Reserve almost the whole balance in a resting limit order.
    engine.create_order(
        OrderRequest(
            symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
            amount=99.0, price=100.0,
        )
    )
    with pytest.raises(InsufficientFunds):
        engine.create_order(
            OrderRequest(
                symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
                amount=10.0, price=100.0,
            )
        )


def test_cancel_releases_reserved_funds(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=100.0))
    order, _ = engine.create_order(
        OrderRequest(
            symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.LIMIT,
            amount=1.0, price=95.0,
        )
    )
    assert engine.balances["EUR"].available < 10_000.0
    engine.cancel_order(order.id)
    assert engine.balances["EUR"].available == pytest.approx(10_000.0)
    assert order.status is OrderStatus.CANCELED


def test_average_entry_price_and_unrealized_pnl(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=100.0))
    engine.create_order(
        OrderRequest(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=1.0)
    )
    engine.process_ticker(make_ticker(bid=119.0, ask=120.0))
    engine.create_order(
        OrderRequest(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=1.0)
    )
    position = engine.positions["BTC/EUR"]
    assert position.entry_price == pytest.approx(110.0)
    engine.process_ticker(make_ticker(bid=129.0, ask=131.0, last=130.0))
    assert engine.unrealized_pnl() == pytest.approx((130.0 - 110.0) * 2.0)


def test_equity_marks_positions_to_market(engine: PaperEngine) -> None:
    engine.process_ticker(make_ticker(bid=99.0, ask=100.0, last=100.0))
    engine.create_order(
        OrderRequest(symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=10.0)
    )
    fee = 10.0 * 100.0 * 0.0025
    assert engine.equity() == pytest.approx(10_000.0 - fee)
    engine.process_ticker(make_ticker(bid=109.0, ask=111.0, last=110.0))
    assert engine.equity() == pytest.approx(10_000.0 - fee + 10.0 * 10.0)
