"""Async entry point: wires config, connector, engine, risk, and strategy together.

Run with::

    python main.py

By default the bot runs in **paper mode** against live WebSocket market data
from the configured exchange (no API keys required). Live trading requires
``PAPER_TRADING=False`` plus valid API keys, and is still gated inside
:class:`~engine.execution.ExecutionRouter`.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from loguru import logger

from config.config import Settings, get_settings
from connectors.ccxt_connector import CCXTProConnector
from engine.execution import ExecutionRouter, LiveTradingBlocked
from engine.models import Fill, OrderRequest, Ticker
from engine.paper_engine import InsufficientFunds, PaperEngine, split_symbol
from risk.risk_manager import RiskManager
from strategies.base import BaseStrategy
from strategies.grid_trading import GridTradingStrategy
from strategies.sma_crossover import SmaCrossoverStrategy
from utils.logging import setup_logging


def build_strategy(settings: Settings, symbol: str) -> BaseStrategy:
    """Instantiate the configured strategy for one symbol."""
    if settings.strategy == "grid":
        return GridTradingStrategy(
            symbol=symbol,
            levels=settings.grid_levels,
            spacing_pct=settings.grid_spacing_pct,
            order_quote_size=settings.grid_order_quote_size,
        )
    return SmaCrossoverStrategy(
        symbol=symbol,
        fast_period=settings.sma_fast_period,
        slow_period=settings.sma_slow_period,
        order_quote_size=settings.sma_order_quote_size,
    )


class TradingApp:
    """Owns the event loop: market data in, risk-checked orders out."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.connector = CCXTProConnector.from_settings(settings)
        self.paper_engine = PaperEngine.create(
            starting_balances={settings.quote_currency: settings.paper_starting_balance},
            maker_fee_rate=settings.maker_fee_rate,
            taker_fee_rate=settings.taker_fee_rate,
        )
        self.risk_manager = RiskManager(settings)
        self.router = ExecutionRouter(settings, self.paper_engine, self.connector)
        self.strategies: dict[str, BaseStrategy] = {
            symbol: build_strategy(settings, symbol) for symbol in settings.symbols
        }
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------
    async def handle_ticker(self, ticker: Ticker) -> None:
        """Process one live ticker end-to-end: match, protect, signal, execute."""
        logger.debug(
            "TICK {} bid {:.2f} / ask {:.2f}", ticker.symbol, ticker.bid, ticker.ask
        )

        # 1. Match resting paper orders against the fresh prices.
        fills = self.paper_engine.process_ticker(ticker)
        await self._notify_fills(fills)

        # 2. Refresh equity / drawdown state.
        self.risk_manager.update_equity(self.paper_engine.equity())

        # 3. Enforce mandatory stop-loss / take-profit exits.
        position = self.paper_engine.positions.get(ticker.symbol)
        if position is not None:
            exit_request = self.risk_manager.check_protective_exit(position, ticker)
            if exit_request is not None:
                await self._execute(exit_request, ticker)

        # 4. Let the strategy react and emit new signals.
        strategy = self.strategies.get(ticker.symbol)
        if strategy is None:
            return
        await strategy.on_ticker(ticker)
        for request in strategy.generate_signals():
            await self._execute(request, ticker, strategy)

    async def _execute(
        self,
        request: OrderRequest,
        ticker: Ticker,
        strategy: BaseStrategy | None = None,
    ) -> None:
        """Risk-check one order request and submit it if approved."""
        decision = self.risk_manager.validate_order(
            request,
            reference_price=ticker.last,
            open_order_count=len(self.paper_engine.open_orders_for(request.symbol)),
        )
        if not decision.approved:
            if isinstance(strategy, GridTradingStrategy):
                strategy.on_order_rejected(request)
            return
        try:
            order, fills = await self.router.submit(decision.request)
        except InsufficientFunds as exc:
            logger.warning("Order not placed ({} {}): {}", request.side, request.symbol, exc)
            if isinstance(strategy, GridTradingStrategy):
                strategy.on_order_rejected(request)
            return
        except LiveTradingBlocked as exc:
            logger.error("SAFETY BLOCK: {}", exc)
            return
        if isinstance(strategy, GridTradingStrategy):
            strategy.register_order(decision.request, order)
        await self._notify_fills(fills)

    async def _notify_fills(self, fills: list[Fill]) -> None:
        """Forward fills to the owning strategy and place follow-up signals."""
        for fill in fills:
            strategy = self.strategies.get(fill.symbol)
            if strategy is None:
                continue
            order = next(
                (o for o in self.paper_engine.closed_orders if o.id == fill.order_id),
                None,
            )
            if order is None:
                continue
            await strategy.on_fill(order)
            ticker = self.paper_engine.last_ticker(fill.symbol)
            if ticker is not None:
                for request in strategy.generate_signals():
                    await self._execute(request, ticker, strategy)

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------
    async def pnl_summary_loop(self) -> None:
        """Periodically log equity, PnL, and open order/position counts."""
        quote = self.settings.quote_currency
        while not self._stop_event.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.settings.pnl_summary_interval
                )
                break
            equity = self.paper_engine.equity()
            logger.info(
                "PNL | equity {:.2f} {} | realized {:+.2f} | unrealized {:+.2f} | "
                "fees {:.2f} | drawdown {:.2%} | open orders {} | positions {}",
                equity,
                quote,
                self.paper_engine.realized_pnl(),
                self.paper_engine.unrealized_pnl(),
                self.paper_engine.total_fees_paid(),
                self.risk_manager.drawdown,
                len(self.paper_engine.open_orders),
                sum(1 for p in self.paper_engine.positions.values() if p.amount > 0),
            )
            for symbol, position in self.paper_engine.positions.items():
                if position.amount <= 0:
                    continue
                last = self.paper_engine.last_ticker(symbol)
                logger.info(
                    "  {} | {:.8f} @ {:.2f} | uPnL {:+.2f} | SL {} | TP {}",
                    symbol,
                    position.amount,
                    position.entry_price,
                    position.unrealized_pnl(last.last) if last else 0.0,
                    f"{position.stop_loss:.2f}" if position.stop_loss else "-",
                    f"{position.take_profit:.2f}" if position.take_profit else "-",
                )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Start all streams and run until interrupted."""
        mode = "PAPER" if self.settings.paper_trading else "LIVE"
        logger.info(
            "Starting trading bot | mode {} | exchange {} | symbols {} | strategy {}",
            mode, self.settings.exchange, self.settings.symbols, self.settings.strategy,
        )
        if mode == "LIVE":
            if not self.settings.live_trading_allowed():
                raise SystemExit(
                    "PAPER_TRADING=False but API keys are missing — refusing to start."
                )
            logger.warning("LIVE TRADING ENABLED — real orders will be placed!")
        else:
            for symbol in self.settings.symbols:
                base, _ = split_symbol(symbol)  # validate symbols early
                logger.info("Paper trading {} with simulated funds", symbol or base)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop_event.set)

        tasks = [
            asyncio.create_task(
                self.connector.watch_ticker(symbol, self.handle_ticker),
                name=f"ticker:{symbol}",
            )
            for symbol in self.settings.symbols
        ]
        tasks.append(asyncio.create_task(self.pnl_summary_loop(), name="pnl-summary"))

        try:
            await self._stop_event.wait()
        finally:
            logger.info("Shutting down…")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.connector.close()
            logger.info(
                "Final equity: {:.2f} {} (realized PnL {:+.2f}, fees {:.2f})",
                self.paper_engine.equity(),
                self.settings.quote_currency,
                self.paper_engine.realized_pnl(),
                self.paper_engine.total_fees_paid(),
            )


def main() -> None:
    """Load settings, configure logging, and run the app."""
    settings = get_settings()
    setup_logging(settings.log_level)
    app = TradingApp(settings)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("Interrupted — goodbye.")


if __name__ == "__main__":
    main()
