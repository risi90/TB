"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from engine.models import Ticker
from engine.paper_engine import PaperEngine


@pytest.fixture()
def engine() -> PaperEngine:
    """A paper engine funded with 10,000 EUR and 0.15%/0.25% fees."""
    return PaperEngine.create(
        starting_balances={"EUR": 10_000.0},
        maker_fee_rate=0.0015,
        taker_fee_rate=0.0025,
    )


def make_ticker(
    bid: float = 99.0, ask: float = 101.0, last: float | None = None,
    symbol: str = "BTC/EUR",
) -> Ticker:
    """Convenience factory for synthetic tickers."""
    return Ticker(symbol=symbol, bid=bid, ask=ask, last=last if last is not None else (bid + ask) / 2)
