"""Unit tests for risk manager validation, drawdown, and SL/TP enforcement."""

from __future__ import annotations

import pytest

from config.config import Settings
from engine.models import OrderRequest, OrderSide, OrderType, Position
from risk.risk_manager import RiskManager
from tests.conftest import make_ticker


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None,
        max_allocation_pct=0.10,
        max_drawdown_pct=0.15,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        max_open_orders=3,
        min_order_notional=5.0,
    )


@pytest.fixture()
def risk(settings: Settings) -> RiskManager:
    manager = RiskManager(settings)
    manager.update_equity(10_000.0)
    return manager


def _buy(amount: float, price: float | None = None) -> OrderRequest:
    return OrderRequest(
        symbol="BTC/EUR",
        side=OrderSide.BUY,
        type=OrderType.LIMIT if price else OrderType.MARKET,
        amount=amount,
        price=price,
    )


def test_approves_order_within_allocation(risk: RiskManager) -> None:
    decision = risk.validate_order(_buy(amount=5.0, price=100.0), reference_price=100.0)
    assert decision.approved


def test_rejects_order_over_max_allocation(risk: RiskManager) -> None:
    # 20 * 100 = 2000 > 10% of 10,000
    decision = risk.validate_order(_buy(amount=20.0, price=100.0), reference_price=100.0)
    assert not decision.approved
    assert "max allocation" in (decision.reason or "")


def test_rejects_below_min_notional(risk: RiskManager) -> None:
    decision = risk.validate_order(_buy(amount=0.01, price=100.0), reference_price=100.0)
    assert not decision.approved
    assert "minimum" in (decision.reason or "")


def test_rejects_when_open_order_cap_reached(risk: RiskManager) -> None:
    decision = risk.validate_order(
        _buy(amount=5.0, price=100.0), reference_price=100.0, open_order_count=3
    )
    assert not decision.approved
    assert "cap" in (decision.reason or "")


def test_drawdown_blocks_new_entries_but_allows_exits(risk: RiskManager) -> None:
    risk.update_equity(8_000.0)  # 20% drawdown from 10k peak
    assert risk.drawdown == pytest.approx(0.20)
    assert risk.drawdown_breached

    entry = risk.validate_order(_buy(amount=5.0, price=100.0), reference_price=100.0)
    assert not entry.approved
    assert "drawdown" in (entry.reason or "")

    exit_request = OrderRequest(
        symbol="BTC/EUR", side=OrderSide.SELL, type=OrderType.MARKET,
        amount=1.0, reduce_only=True,
    )
    assert risk.validate_order(exit_request, reference_price=100.0).approved


def test_mandatory_sl_tp_stamped_on_entries(risk: RiskManager) -> None:
    decision = risk.validate_order(_buy(amount=5.0, price=100.0), reference_price=100.0)
    assert decision.approved
    assert decision.request.stop_loss == pytest.approx(98.0)     # -2%
    assert decision.request.take_profit == pytest.approx(104.0)  # +4%


def test_strategy_provided_sl_tp_is_preserved(risk: RiskManager) -> None:
    request = _buy(amount=5.0, price=100.0)
    request.stop_loss = 90.0
    request.take_profit = 120.0
    decision = risk.validate_order(request, reference_price=100.0)
    assert decision.request.stop_loss == pytest.approx(90.0)
    assert decision.request.take_profit == pytest.approx(120.0)


def test_protective_exit_on_stop_loss(risk: RiskManager) -> None:
    position = Position(
        symbol="BTC/EUR", amount=1.0, entry_price=100.0,
        stop_loss=98.0, take_profit=104.0,
    )
    assert risk.check_protective_exit(position, make_ticker(bid=99.0, ask=99.5)) is None
    exit_request = risk.check_protective_exit(position, make_ticker(bid=97.9, ask=98.4))
    assert exit_request is not None
    assert exit_request.side is OrderSide.SELL
    assert exit_request.type is OrderType.MARKET
    assert exit_request.amount == pytest.approx(1.0)
    assert exit_request.reduce_only


def test_protective_exit_on_take_profit(risk: RiskManager) -> None:
    position = Position(
        symbol="BTC/EUR", amount=1.0, entry_price=100.0,
        stop_loss=98.0, take_profit=104.0,
    )
    exit_request = risk.check_protective_exit(position, make_ticker(bid=104.5, ask=105.0))
    assert exit_request is not None
    assert exit_request.reduce_only


def test_rejects_non_positive_amount(risk: RiskManager) -> None:
    assert not risk.validate_order(_buy(amount=0.0, price=100.0), reference_price=100.0).approved
