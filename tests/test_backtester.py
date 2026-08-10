"""Backtester tests: data pagination/caching, fill rules, and metrics."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from config.config import Settings
from backtester.data_loader import OHLCVDataLoader, candles_from_df
from backtester.engine import BacktestEngine
from backtester.metrics import (
    compute_metrics,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    trade_stats,
)
from backtester.models import BacktestResult, Candle, TradeRecord, timeframe_to_ms
from engine.models import OrderBook, OrderRequest, OrderSide, OrderType, Ticker
from strategies.base import BaseStrategy
from strategies.grid_trading import GridTradingStrategy

_T0 = 1_700_000_000_000  # ms
_MINUTE = 60_000


# ---------------------------------------------------------------------------
# Data loader: pagination & caching
# ---------------------------------------------------------------------------
class FakeExchange:
    """Deterministic fetch_ohlcv stub with page-size limits and a call counter."""

    def __init__(self, rows: list[list[float]]) -> None:
        self.rows = rows
        self.fetch_calls = 0

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        since: int | None = None,
        limit: int | None = 1000,
    ) -> list[list[float]]:
        self.fetch_calls += 1
        since = since or 0
        return [r for r in self.rows if r[0] >= since][: limit or 1000]


def _make_rows(count: int, start: int = _T0) -> list[list[float]]:
    return [
        [start + i * _MINUTE, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0]
        for i in range(count)
    ]


def test_loader_paginates_and_caches(tmp_path: Path) -> None:
    fake = FakeExchange(_make_rows(2500))
    loader = OHLCVDataLoader(exchange_id="bitvavo", cache_dir=tmp_path, exchange=fake)
    start, end = _T0, _T0 + 2499 * _MINUTE

    df = loader.load("BTC/EUR", "1m", start, end)
    assert len(df) == 2500
    assert df["timestamp"].iloc[0] == start
    assert df["timestamp"].iloc[-1] == end
    assert df["timestamp"].is_monotonic_increasing
    assert fake.fetch_calls == 3  # 1000 + 1000 + 500 with page size 1000

    # Second identical load is served entirely from the SQLite cache.
    df2 = loader.load("BTC/EUR", "1m", start, end)
    assert fake.fetch_calls == 3
    assert len(df2) == 2500

    # Cache file is keyed exchange_symbol_timeframe.
    assert loader.cache_path("BTC/EUR", "1m").name == "bitvavo_BTC-EUR_1m.sqlite"
    assert loader.cache_path("BTC/EUR", "1m").exists()


def test_loader_fetches_only_missing_tail(tmp_path: Path) -> None:
    fake = FakeExchange(_make_rows(3000))
    loader = OHLCVDataLoader(cache_dir=tmp_path, exchange=fake)

    loader.load("BTC/EUR", "1m", _T0, _T0 + 999 * _MINUTE)
    calls_after_first = fake.fetch_calls

    # Extending the range only downloads the uncached tail span.
    df = loader.load("BTC/EUR", "1m", _T0, _T0 + 1499 * _MINUTE)
    assert len(df) == 1500
    assert fake.fetch_calls == calls_after_first + 1


def test_loader_rejects_inverted_range(tmp_path: Path) -> None:
    loader = OHLCVDataLoader(cache_dir=tmp_path, exchange=FakeExchange([]))
    with pytest.raises(ValueError):
        loader.load("BTC/EUR", "1m", _T0, _T0)


def test_timeframe_to_ms() -> None:
    assert timeframe_to_ms("5m") == 300_000
    assert timeframe_to_ms("1d") == 86_400_000
    with pytest.raises(ValueError):
        timeframe_to_ms("3w")


# ---------------------------------------------------------------------------
# Backtest engine: fill rules on synthetic candles
# ---------------------------------------------------------------------------
def _candle(i: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(timestamp=_T0 / 1000 + i * 60, open=o, high=h, low=low, close=c)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


class OneShotMarketBuy(BaseStrategy):
    """Test helper: emits a single market buy on the first candle close."""

    def __init__(self, symbol: str, amount: float) -> None:
        super().__init__(symbol)
        self._amount = amount
        self._emitted = False
        self._pending: list[OrderRequest] = []

    async def on_ticker(self, ticker: Ticker) -> None:
        if not self._emitted:
            self._emitted = True
            self._pending.append(
                OrderRequest(
                    symbol=self.symbol, side=OrderSide.BUY,
                    type=OrderType.MARKET, amount=self._amount,
                )
            )

    async def on_orderbook(self, orderbook: OrderBook) -> None:
        """Unused."""

    def generate_signals(self) -> list[OrderRequest]:
        signals, self._pending = self._pending, []
        return signals


async def test_grid_limit_fills_when_candle_low_reaches_price() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=2, spacing_pct=0.01, order_quote_size=100.0
    )
    engine = BacktestEngine(
        strategy, _settings(), initial_capital=10_000.0,
        maker_fee_rate=0.0015, taker_fee_rate=0.0025, slippage_rate=0.0,
    )
    candles = [
        _candle(0, 100.0, 100.0, 100.0, 100.0),  # anchor -> buys rest at 99 / 98
        _candle(1, 100.0, 100.0, 99.5, 100.0),   # low above 99 -> no fill
        _candle(2, 100.0, 100.5, 98.5, 100.5),   # low crosses 99 -> fill; then sell 100 fills
    ]
    result = await engine.run(candles, timeframe="1m")

    buys = [t for t in result.trades if t.side == "buy"]
    sells = [t for t in result.trades if t.side == "sell"]
    assert len(buys) == 1
    assert buys[0].price == pytest.approx(99.0)  # filled AT the limit price
    assert buys[0].type == "limit"
    assert buys[0].fee == pytest.approx(buys[0].amount * 99.0 * 0.0015)  # maker

    # The paired grid sell at 100 fills within the same candle's range.
    assert len(sells) == 1
    assert sells[0].price == pytest.approx(100.0)
    assert sells[0].realized_pnl == pytest.approx(sells[0].amount * 1.0)
    assert strategy.total_cycles == 1


async def test_no_fill_when_candle_range_misses_limit() -> None:
    strategy = GridTradingStrategy(
        "BTC/EUR", levels=1, spacing_pct=0.05, order_quote_size=100.0
    )
    engine = BacktestEngine(
        strategy, _settings(), initial_capital=10_000.0, slippage_rate=0.0
    )
    candles = [
        _candle(0, 100.0, 100.0, 100.0, 100.0),  # buy rests at 95
        _candle(1, 100.0, 102.0, 96.0, 101.0),   # low 96 > 95 -> stays open
    ]
    result = await engine.run(candles, timeframe="1m")
    assert result.trades == []


async def test_market_order_pays_slippage_and_taker_fee() -> None:
    strategy = OneShotMarketBuy("BTC/EUR", amount=1.0)
    engine = BacktestEngine(
        strategy, _settings(max_allocation_pct=1.0), initial_capital=10_000.0,
        taker_fee_rate=0.0025, slippage_rate=0.001,
    )
    result = await engine.run([_candle(0, 100.0, 100.0, 100.0, 100.0)], "1m")

    [trade] = result.trades
    assert trade.side == "buy"
    assert trade.price == pytest.approx(100.0 * 1.001)  # 0.1% adverse slippage
    assert trade.fee == pytest.approx(1.0 * 100.1 * 0.0025)  # taker fee on slipped cost


async def test_stop_loss_triggers_intra_candle() -> None:
    strategy = OneShotMarketBuy("BTC/EUR", amount=1.0)
    engine = BacktestEngine(
        strategy,
        _settings(max_allocation_pct=1.0, stop_loss_pct=0.05, take_profit_pct=0.5),
        initial_capital=10_000.0,
        slippage_rate=0.0,
    )
    candles = [
        _candle(0, 100.0, 100.0, 100.0, 100.0),  # buy at 100, SL at 95
        _candle(1, 100.0, 101.0, 94.0, 100.0),   # spikes down to 94 intra-candle
    ]
    result = await engine.run(candles, timeframe="1m")

    sells = [t for t in result.trades if t.side == "sell"]
    assert len(sells) == 1
    assert sells[0].price == pytest.approx(94.0)  # exit at the touched low
    assert sells[0].realized_pnl == pytest.approx(-6.0)
    # Even though the candle CLOSED back at 100, the intra-candle stop fired.


async def test_equity_curve_and_benchmark_tracked_per_candle() -> None:
    strategy = OneShotMarketBuy("BTC/EUR", amount=10.0)
    taker = 0.0025
    engine = BacktestEngine(
        strategy, _settings(max_allocation_pct=1.0, stop_loss_pct=0.9, take_profit_pct=0.9),
        initial_capital=10_000.0, taker_fee_rate=taker, slippage_rate=0.0,
    )
    candles = [
        _candle(0, 100.0, 100.0, 100.0, 100.0),
        _candle(1, 100.0, 110.0, 100.0, 110.0),
        _candle(2, 110.0, 120.0, 110.0, 120.0),
    ]
    result = await engine.run(candles, timeframe="1m")

    assert len(result.timestamps) == len(candles)
    assert len(result.equity) == len(candles)
    fee = 10.0 * 100.0 * taker
    assert result.equity[0] == pytest.approx(10_000.0 - fee)
    assert result.equity[-1] == pytest.approx(10_000.0 - fee + 10.0 * 20.0)
    # Benchmark: all-in at first open with taker fee, marked at closes.
    bh_units = 10_000.0 * (1 - taker) / 100.0
    assert result.benchmark[-1] == pytest.approx(bh_units * 120.0)
    assert result.total_fees == pytest.approx(fee)


# ---------------------------------------------------------------------------
# Metrics against known expected outputs
# ---------------------------------------------------------------------------
def test_max_drawdown_known_series() -> None:
    equity = [100.0, 120.0, 90.0, 100.0, 130.0]
    timestamps = [0.0, 100.0, 200.0, 300.0, 400.0]
    depth, duration = max_drawdown(equity, timestamps)
    assert depth == pytest.approx(0.25)  # 120 -> 90
    assert duration == pytest.approx(300.0)  # peak at t=100, recovered at t=400


def test_max_drawdown_never_recovered_runs_to_end() -> None:
    equity = [100.0, 120.0, 80.0, 85.0]
    timestamps = [0.0, 10.0, 20.0, 30.0]
    depth, duration = max_drawdown(equity, timestamps)
    assert depth == pytest.approx(1 - 80 / 120)
    assert duration == pytest.approx(20.0)  # t=10 until the end (t=30)


def test_max_drawdown_monotonic_series_is_zero() -> None:
    assert max_drawdown([100.0, 110.0, 120.0], [0.0, 1.0, 2.0]) == (0.0, 0.0)


def test_sharpe_ratio_known_values() -> None:
    # mean 0.02, stdev 0.01, 1 period/year -> Sharpe exactly 2.0
    assert sharpe_ratio([0.01, 0.02, 0.03], periods_per_year=1.0) == pytest.approx(2.0)
    assert sharpe_ratio([0.01, 0.01, 0.01], periods_per_year=252.0) == 0.0  # zero vol
    assert sharpe_ratio([0.05], periods_per_year=252.0) == 0.0  # too short


def test_sortino_ratio_known_values() -> None:
    # returns [0.2, -0.1]: mean 0.05, downside dev sqrt(0.01/2)
    expected = 0.05 / math.sqrt(0.01 / 2.0)
    assert sortino_ratio([0.2, -0.1], periods_per_year=1.0) == pytest.approx(expected)
    assert sortino_ratio([0.1, 0.2], periods_per_year=1.0) == math.inf  # no downside


def test_trade_stats_win_rate_and_profit_factor() -> None:
    trades = [
        TradeRecord(0, "buy", "limit", 100.0, 1.0, 0.1),
        TradeRecord(1, "sell", "limit", 110.0, 1.0, 0.1, realized_pnl=10.0),
        TradeRecord(2, "sell", "market", 95.0, 1.0, 0.1, realized_pnl=-5.0),
        TradeRecord(3, "sell", "limit", 115.0, 1.0, 0.1, realized_pnl=15.0),
    ]
    win_rate, profit_factor, wins, losses = trade_stats(trades)
    assert win_rate == pytest.approx(100.0 * 2 / 3)
    assert profit_factor == pytest.approx(25.0 / 5.0)
    assert (wins, losses) == (2, 1)


def test_compute_metrics_end_to_end() -> None:
    day = 86_400.0
    result = BacktestResult(
        symbol="BTC/EUR", timeframe="1d", initial_capital=10_000.0,
        timestamps=[0.0, day, 2 * day, 3 * day],
        equity=[10_000.0, 10_500.0, 10_200.0, 11_000.0],
        benchmark=[10_000.0, 10_100.0, 10_050.0, 10_400.0],
        close_prices=[100.0, 101.0, 100.5, 104.0],
        trades=[
            TradeRecord(0, "buy", "limit", 100.0, 1.0, 1.5),
            TradeRecord(day, "sell", "limit", 105.0, 1.0, 1.5, realized_pnl=5.0),
        ],
        total_fees=3.0,
    )
    metrics = compute_metrics(result)
    assert metrics.total_pnl == pytest.approx(1_000.0)
    assert metrics.total_return_pct == pytest.approx(10.0)
    assert metrics.benchmark_return_pct == pytest.approx(4.0)
    assert metrics.max_drawdown_pct == pytest.approx(100.0 * (1 - 10_200 / 10_500))
    assert metrics.max_drawdown_duration_s == pytest.approx(2 * day)
    assert metrics.win_rate_pct == pytest.approx(100.0)
    assert metrics.total_trades == 2
    assert metrics.total_fees == pytest.approx(3.0)
    assert metrics.cagr_pct > 0
    assert metrics.final_equity == pytest.approx(11_000.0)
