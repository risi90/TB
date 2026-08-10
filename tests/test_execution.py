"""Tests for the execution router's live-trading safety hardblock."""

from __future__ import annotations

import pytest

from config.config import Settings
from engine.execution import ExecutionRouter, LiveTradingBlocked
from engine.models import OrderRequest, OrderSide, OrderStatus, OrderType
from engine.paper_engine import PaperEngine
from tests.conftest import make_ticker


def _request() -> OrderRequest:
    return OrderRequest(
        symbol="BTC/EUR", side=OrderSide.BUY, type=OrderType.MARKET, amount=1.0
    )


@pytest.fixture()
def paper_engine() -> PaperEngine:
    engine = PaperEngine.create({"EUR": 10_000.0})
    engine.process_ticker(make_ticker(bid=99.0, ask=100.0))
    return engine


async def test_paper_mode_routes_to_paper_engine(paper_engine: PaperEngine) -> None:
    settings = Settings(_env_file=None)  # paper_trading defaults to True
    router = ExecutionRouter(settings, paper_engine, connector=None)
    assert router.is_paper
    order, fills = await router.submit(_request())
    assert order.status is OrderStatus.FILLED
    assert len(fills) == 1
    assert paper_engine.fills  # order landed in the simulator, not an exchange


async def test_live_mode_without_keys_is_hardblocked(paper_engine: PaperEngine) -> None:
    settings = Settings(_env_file=None, paper_trading=False, api_key="", api_secret="")
    assert not settings.live_trading_allowed()
    router = ExecutionRouter(settings, paper_engine, connector=None)
    with pytest.raises(LiveTradingBlocked):
        await router.submit(_request())
    assert not paper_engine.fills  # nothing executed anywhere


async def test_live_mode_with_keys_but_no_connector_is_blocked(
    paper_engine: PaperEngine,
) -> None:
    settings = Settings(
        _env_file=None, paper_trading=False, api_key="key", api_secret="secret"
    )
    assert settings.live_trading_allowed()
    router = ExecutionRouter(settings, paper_engine, connector=None)
    with pytest.raises(LiveTradingBlocked):
        await router.submit(_request())


def test_paper_trading_defaults_to_true() -> None:
    settings = Settings(_env_file=None)
    assert settings.paper_trading is True
    assert not settings.live_trading_allowed()
