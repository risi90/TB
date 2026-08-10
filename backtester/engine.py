"""Candle-replay backtest engine reusing the live strategy and risk stack.

The engine replays historical OHLCV candles through the exact same components
the live bot uses — :class:`~engine.paper_engine.PaperEngine` for order
matching and account bookkeeping, :class:`~risk.risk_manager.RiskManager` for
validation and SL/TP enforcement, and unmodified strategy classes — so a
backtested strategy behaves identically in paper/live trading.

Intra-candle matching
---------------------
Each candle is expanded into a deterministic price path (up candles:
``open → low → high → close``, down candles: ``open → high → low → close``)
and every point is fed to the matching engine as a zero-spread tick:

* resting **limit orders** (grid rungs) fill at their limit price when the
  candle's range crosses it, paying the **maker** fee;
* **market orders** (entries and stop-loss/take-profit exits) fill at the
  current path price adjusted by the configurable **slippage** model, paying
  the **taker** fee;
* stop-loss / take-profit triggers are evaluated at every path point, so an
  intra-candle spike to the stop fires even if the close recovers.

Strategies only observe the candle **close** (one ``on_ticker`` per candle),
mirroring how indicators are computed live.

A parallel buy-&-hold benchmark (all-in at the first candle's open, taker fee
applied) is tracked for comparison.
"""

from __future__ import annotations

from typing import Iterator, Sequence

from loguru import logger

from config.config import Settings
from engine.models import Fill, OrderRequest, OrderSide, Ticker
from engine.paper_engine import InsufficientFunds, PaperEngine, PaperEngineError, split_symbol
from risk.risk_manager import RiskManager
from backtester.models import BacktestResult, Candle, TradeRecord
from strategies.base import BaseStrategy
from strategies.grid_trading import GridTradingStrategy


def _candle_path(candle: Candle) -> Iterator[tuple[float, bool]]:
    """Yield ``(price, is_close)`` points approximating intra-candle movement."""
    if candle.close >= candle.open:
        path = (candle.open, candle.low, candle.high, candle.close)
    else:
        path = (candle.open, candle.high, candle.low, candle.close)
    for i, price in enumerate(path):
        yield price, i == len(path) - 1


class BacktestEngine:
    """Replays candles through a strategy with realistic order matching.

    Args:
        strategy: A fresh strategy instance (its symbol defines the market).
        settings: Settings supplying risk limits (SL/TP, allocation caps).
        initial_capital: Starting quote-currency funds.
        maker_fee_rate: Fee fraction for resting limit fills.
        taker_fee_rate: Fee fraction for market / crossing fills.
        slippage_rate: Adverse slippage applied to market executions
            (default 0.05%).
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        settings: Settings,
        initial_capital: float = 10_000.0,
        maker_fee_rate: float = 0.0015,
        taker_fee_rate: float = 0.0025,
        slippage_rate: float = 0.0005,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        self._strategy = strategy
        self._settings = settings
        self._initial_capital = initial_capital
        self._maker_fee_rate = maker_fee_rate
        self._taker_fee_rate = taker_fee_rate
        self._slippage_rate = slippage_rate
        # Run state (created in run()):
        self._paper: PaperEngine | None = None
        self._risk: RiskManager | None = None
        self._trades: list[TradeRecord] = []
        self._realized_seen = 0.0

    async def run(self, candles: Sequence[Candle], timeframe: str = "1h") -> BacktestResult:
        """Replay ``candles`` sequentially and return the full result.

        Raises:
            ValueError: If no candles are provided.
        """
        if not candles:
            raise ValueError("cannot backtest an empty candle series")

        symbol = self._strategy.symbol
        _, quote = split_symbol(symbol)
        self._paper = PaperEngine.create(
            starting_balances={quote: self._initial_capital},
            maker_fee_rate=self._maker_fee_rate,
            taker_fee_rate=self._taker_fee_rate,
            slippage_rate=self._slippage_rate,
        )
        self._risk = RiskManager(self._settings)
        self._trades = []
        self._realized_seen = 0.0

        result = BacktestResult(
            symbol=symbol, timeframe=timeframe, initial_capital=self._initial_capital
        )
        bh_units = (
            self._initial_capital * (1.0 - self._taker_fee_rate) / candles[0].open
        )

        logger.info(
            "Backtesting {} on {} candles ({}) | capital {:.2f} {} | "
            "fees {:.3%}/{:.3%} | slippage {:.3%}",
            self._strategy.name, len(candles), timeframe,
            self._initial_capital, quote,
            self._maker_fee_rate, self._taker_fee_rate, self._slippage_rate,
        )

        for candle in candles:
            await self._process_candle(candle, symbol)
            result.timestamps.append(candle.timestamp)
            result.equity.append(self._paper.equity())
            result.benchmark.append(bh_units * candle.close)
            result.close_prices.append(candle.close)

        result.trades = self._trades
        result.total_fees = self._paper.total_fees_paid()
        logger.info(
            "Backtest done | final equity {:.2f} {} | {} trades | fees {:.2f}",
            result.final_equity, quote, len(result.trades), result.total_fees,
        )
        return result

    # ------------------------------------------------------------------
    # Candle processing
    # ------------------------------------------------------------------
    async def _process_candle(self, candle: Candle, symbol: str) -> None:
        assert self._paper is not None and self._risk is not None
        for price, is_close in _candle_path(candle):
            ticker = Ticker(
                symbol=symbol, bid=price, ask=price, last=price,
                timestamp=candle.timestamp,
            )
            fills = self._paper.process_ticker(ticker)
            await self._on_fills(fills, ticker)

            self._risk.update_equity(self._paper.equity())

            position = self._paper.positions.get(symbol)
            if position is not None:
                exit_request = self._risk.check_protective_exit(position, ticker)
                if exit_request is not None:
                    self._cancel_resting_sells(symbol)
                    await self._submit(exit_request, ticker)

            if is_close:
                await self._strategy.on_ticker(ticker)
                for request in self._strategy.generate_signals():
                    await self._submit(request, ticker)

    def _cancel_resting_sells(self, symbol: str) -> None:
        """Free reserved inventory before a protective full-position exit."""
        assert self._paper is not None
        for order in self._paper.open_orders_for(symbol):
            if order.side is OrderSide.SELL:
                self._paper.cancel_order(order.id)
                if isinstance(self._strategy, GridTradingStrategy):
                    self._strategy.on_order_canceled(order)

    async def _submit(self, request: OrderRequest, ticker: Ticker) -> None:
        """Risk-check and execute one order request inside the simulation."""
        assert self._paper is not None and self._risk is not None
        decision = self._risk.validate_order(
            request,
            reference_price=ticker.last,
            open_order_count=len(self._paper.open_orders_for(request.symbol)),
        )
        if not decision.approved:
            if isinstance(self._strategy, GridTradingStrategy):
                self._strategy.on_order_rejected(request)
            return
        try:
            order, fills = self._paper.create_order(decision.request)
        except (InsufficientFunds, PaperEngineError) as exc:
            logger.debug("Backtest order not placed: {}", exc)
            if isinstance(self._strategy, GridTradingStrategy):
                self._strategy.on_order_rejected(request)
            return
        if isinstance(self._strategy, GridTradingStrategy):
            self._strategy.register_order(decision.request, order)
        await self._on_fills(fills, ticker)

    async def _on_fills(self, fills: list[Fill], ticker: Ticker) -> None:
        """Record trades, notify the strategy, and execute follow-up signals."""
        assert self._paper is not None
        for fill in fills:
            self._record_trade(fill, ticker.timestamp)
            order = next(
                (
                    o
                    for o in reversed(self._paper.closed_orders)
                    if o.id == fill.order_id
                ),
                None,
            )
            if order is None:
                continue
            await self._strategy.on_fill(order)
            for request in self._strategy.generate_signals():
                await self._submit(request, ticker)

    def _record_trade(self, fill: Fill, timestamp: float) -> None:
        assert self._paper is not None
        realized_delta = 0.0
        if fill.side is OrderSide.SELL:
            realized_now = self._paper.realized_pnl()
            realized_delta = realized_now - self._realized_seen
            self._realized_seen = realized_now
        order = next(
            (o for o in reversed(self._paper.closed_orders) if o.id == fill.order_id),
            None,
        )
        self._trades.append(
            TradeRecord(
                timestamp=timestamp,
                side=fill.side.value,
                type=order.type.value if order is not None else "market",
                price=fill.price,
                amount=fill.amount,
                fee=fill.fee,
                realized_pnl=realized_delta,
            )
        )
