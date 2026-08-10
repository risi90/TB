"""Async entry point: wires config, storage, connector, engine, risk, and strategies.

Run with::

    python main.py

By default the bot runs in **paper mode** against live WebSocket market data
(no API keys required). State — balances, positions, orders, grid levels, and
the equity curve — is persisted to SQLite (``DB_PATH``) so the bot resumes
seamlessly after restarts, and the Streamlit dashboard
(``streamlit run dashboard/app.py``) reads the same database and can adjust
runtime settings through the ``bot_config`` table while the bot is running.
"""

from __future__ import annotations

import asyncio
import json
import time

from loguru import logger

from config.config import Settings, get_settings
from connectors.ccxt_connector import CCXTProConnector
from engine.candle_aggregator import CandleAggregator
from engine.execution import ExecutionRouter, LiveTradingBlocked
from engine.models import Fill, OrderRequest, OrderSide, Ticker
from engine.paper_engine import InsufficientFunds, PaperEngine, PaperEngineError
from risk.risk_manager import RiskManager
from storage.config_sync import ConfigChange, ConfigSync
from storage.db import Database
from storage.persistence import PersistenceManager
from strategies.base import BaseStrategy
from strategies.grid_trading import GridTradingStrategy
from strategies.sma_crossover import SmaCrossoverStrategy
from utils.lifecycle import ShutdownManager
from utils.logging import setup_logging
from utils.notifications import Notifier

#: bot_config keys that require strategies (and their orders) to be rebuilt.
_STRATEGY_KEYS = frozenset(
    {"symbols", "grid_levels", "grid_spacing_pct", "grid_order_quote_size"}
)


def build_strategy(settings: Settings, symbol: str) -> BaseStrategy:
    """Instantiate the configured strategy for one symbol."""
    if settings.strategy == "grid":
        return GridTradingStrategy(
            symbol=symbol,
            levels=settings.grid_levels,
            spacing_pct=settings.grid_spacing_pct,
            order_quote_size=settings.grid_order_quote_size,
            auto_reanchor=settings.grid_auto_reanchor,
            reanchor_factor=settings.grid_reanchor_factor,
            max_inventory_quote=settings.grid_max_inventory_quote or None,
        )
    return SmaCrossoverStrategy(
        symbol=symbol,
        fast_period=settings.sma_fast_period,
        slow_period=settings.sma_slow_period,
        order_quote_size=settings.sma_order_quote_size,
    )


class TradingApp:
    """Owns the event loop: market data in, risk-checked persisted orders out."""

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
        self.db = Database(settings.db_path)
        self.persistence = PersistenceManager(self.db)
        self.config_sync = ConfigSync(self.db, settings)
        self.lifecycle = ShutdownManager()
        self.notifier = Notifier.from_settings(settings)
        self.strategies: dict[str, BaseStrategy] = {}
        self._aggregators: dict[str, CandleAggregator] = {}
        self._order_strategy: dict[str, str] = {}
        self._ticker_tasks: dict[str, asyncio.Task[None]] = {}
        self._aux_tasks: list[asyncio.Task[None]] = []
        self._paused = False
        self._drawdown_alerted = False

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
        if fills:
            await self._after_fills(fills)

        # 2. Refresh equity / drawdown state (and alert on breaker flips).
        self.risk_manager.update_equity(self.paper_engine.equity())
        await self._check_drawdown_transition()

        # 3. Enforce mandatory stop-loss / take-profit exits (even when paused).
        position = self.paper_engine.positions.get(ticker.symbol)
        if position is not None:
            exit_request = self.risk_manager.check_protective_exit(position, ticker)
            if exit_request is not None:
                trigger = (
                    "STOP-LOSS"
                    if position.stop_loss is not None and ticker.bid <= position.stop_loss
                    else "TAKE-PROFIT"
                )
                emoji = "🛑" if trigger == "STOP-LOSS" else "🎯"
                await self.notifier.send(
                    f"{emoji} {trigger} triggered on {ticker.symbol} @ {ticker.bid:.2f} "
                    f"(entry {position.entry_price:.2f})"
                )
                await self._execute(exit_request, ticker)

        # 4. Let the strategy react — unless the bot is paused.
        if self._paused:
            return
        strategy = self.strategies.get(ticker.symbol)
        if strategy is None:
            return
        await strategy.on_ticker(ticker)
        aggregator = self._aggregators.get(ticker.symbol)
        if aggregator is not None:
            bar = aggregator.add_tick(ticker.last, ticker.timestamp)
            if bar is not None:
                await strategy.on_bar_close(bar)
        await self._process_cancellations(strategy, ticker.symbol)
        for request in strategy.generate_signals():
            await self._execute(request, ticker, strategy)

    async def _check_drawdown_transition(self) -> None:
        """Notify when the max-drawdown circuit breaker trips or recovers."""
        breached = self.risk_manager.drawdown_breached
        if breached and not self._drawdown_alerted:
            self._drawdown_alerted = True
            message = (
                f"🚨 DRAWDOWN CIRCUIT BREAKER: portfolio down "
                f"{self.risk_manager.drawdown:.1%} from peak — new entries blocked"
            )
            logger.warning(message)
            await self.notifier.send(message)
        elif not breached and self._drawdown_alerted:
            self._drawdown_alerted = False
            await self.notifier.send(
                f"✅ Drawdown recovered to {self.risk_manager.drawdown:.1%} — "
                f"entries re-enabled"
            )

    async def _process_cancellations(self, strategy: BaseStrategy, symbol: str) -> None:
        """Cancel orders a strategy no longer wants (e.g. after a grid re-anchor)."""
        cancel_ids = strategy.generate_cancellations()
        if not cancel_ids:
            return
        for order_id in cancel_ids:
            try:
                await self.router.cancel(order_id, symbol)
            except Exception as exc:
                logger.warning("Cancel of {} failed: {}", order_id, exc)
                continue
            order = next(
                (o for o in reversed(self.paper_engine.closed_orders) if o.id == order_id),
                None,
            )
            if order is not None:
                await self.persistence.persist_order(
                    order, self._order_strategy.pop(order_id, None)
                )
        await self.persistence.persist_account(self.paper_engine)
        if isinstance(strategy, GridTradingStrategy):
            await self._persist_grid(strategy)

    async def _execute(
        self,
        request: OrderRequest,
        ticker: Ticker,
        strategy: BaseStrategy | None = None,
    ) -> None:
        """Risk-check one order request, submit it, and persist the outcome."""
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
        except (InsufficientFunds, PaperEngineError) as exc:
            logger.warning("Order not placed ({} {}): {}", request.side, request.symbol, exc)
            if isinstance(strategy, GridTradingStrategy):
                strategy.on_order_rejected(request)
            return
        except LiveTradingBlocked as exc:
            logger.error("SAFETY BLOCK: {}", exc)
            return

        strategy_id = strategy.strategy_id if strategy is not None else None
        if strategy_id is not None:
            self._order_strategy[order.id] = strategy_id
        if isinstance(strategy, GridTradingStrategy):
            strategy.register_order(decision.request, order)

        await self.persistence.persist_order(order, strategy_id)
        await self.persistence.persist_account(self.paper_engine)
        if isinstance(strategy, GridTradingStrategy):
            await self._persist_grid(strategy)
        if fills:
            await self._after_fills(fills)

    async def _after_fills(self, fills: list[Fill]) -> None:
        """Persist fill effects, notify strategies, and place follow-up orders."""
        touched: set[str] = set()
        for fill in fills:
            order = next(
                (o for o in self.paper_engine.closed_orders if o.id == fill.order_id),
                None,
            )
            if order is None:
                continue
            await self.persistence.persist_order(
                order, self._order_strategy.get(order.id)
            )
            emoji = "🟢" if fill.side is OrderSide.BUY else "🔴"
            await self.notifier.send(
                f"{emoji} FILL {fill.side.upper()} {fill.amount:.6f} {fill.symbol} "
                f"@ {fill.price:.2f} (fee {fill.fee:.4f})"
            )
            strategy = self.strategies.get(fill.symbol)
            if strategy is None:
                continue
            touched.add(fill.symbol)
            await strategy.on_fill(order)
            ticker = self.paper_engine.last_ticker(fill.symbol)
            if ticker is not None:
                for request in strategy.generate_signals():
                    await self._execute(request, ticker, strategy)

        await self.persistence.persist_account(self.paper_engine)
        for symbol in touched:
            strategy = self.strategies.get(symbol)
            if isinstance(strategy, GridTradingStrategy):
                await self._persist_grid(strategy)

    async def _persist_grid(self, strategy: GridTradingStrategy) -> None:
        """Write one grid strategy's serialized state to SQLite."""
        config, levels = strategy.to_state()
        await self.db.save_grid_state(
            strategy_id=strategy.strategy_id,
            symbol=strategy.symbol,
            grid_config_json=json.dumps(config),
            active_levels_json=json.dumps(levels),
        )

    # ------------------------------------------------------------------
    # Dynamic configuration
    # ------------------------------------------------------------------
    async def config_poll_loop(self) -> None:
        """Poll ``bot_config`` and apply runtime changes without a restart."""
        while True:
            await asyncio.sleep(self.settings.config_poll_interval)
            try:
                changes = await self.config_sync.poll()
            except Exception as exc:
                logger.error("Config poll failed: {}", exc)
                continue
            if changes:
                await self._apply_config_changes(changes)

    async def _apply_config_changes(self, changes: list[ConfigChange]) -> None:
        """React to applied runtime configuration changes."""
        keys = {change.key for change in changes}

        if "bot_status" in keys:
            status = self.config_sync.bot_status
            if status == "stopped":
                self.lifecycle.request("bot_status set to 'stopped'")
                return
            if status == "paused" and not self._paused:
                self._paused = True
                logger.warning("Bot PAUSED — no new entries (exits stay active)")
            elif status == "running" and self._paused:
                self._paused = False
                logger.info("Bot RESUMED — strategies active again")

        if "exchange" in keys:
            logger.warning(
                "Exchange changed to {} — takes effect on next process restart",
                self.settings.exchange,
            )

        if keys & _STRATEGY_KEYS:
            await self._rebuild_strategies()

    async def _rebuild_strategies(self) -> None:
        """Tear down and rebuild strategies after symbol/grid parameter changes.

        Open orders of affected symbols are canceled (releasing reserved
        funds), persisted grid state is discarded, and fresh strategies are
        built; new symbols get ticker streams, removed symbols lose theirs.
        """
        desired = list(self.settings.symbols)
        logger.info("Rebuilding strategies for symbols {}", desired)

        for symbol in [s for s in self._ticker_tasks if s not in desired]:
            task = self._ticker_tasks.pop(symbol)
            task.cancel()
            await self._retire_strategy(symbol)

        for symbol in desired:
            await self._retire_strategy(symbol)
            self.strategies[symbol] = build_strategy(self.settings, symbol)
            self._aggregators[symbol] = CandleAggregator(self.settings.bar_timeframe)
            self._sync_sma_position(symbol)
            if symbol not in self._ticker_tasks:
                self._ticker_tasks[symbol] = asyncio.create_task(
                    self.connector.watch_ticker(symbol, self.handle_ticker),
                    name=f"ticker:{symbol}",
                )

    async def _retire_strategy(self, symbol: str) -> None:
        """Cancel a symbol's open orders and drop its persisted grid state."""
        strategy = self.strategies.pop(symbol, None)
        self._aggregators.pop(symbol, None)
        for order in self.paper_engine.open_orders_for(symbol):
            try:
                await self.router.cancel(order.id, symbol)
            except Exception as exc:
                logger.warning("Failed to cancel {}: {}", order.id, exc)
                continue
            await self.persistence.persist_order(
                order, self._order_strategy.pop(order.id, None)
            )
        await self.persistence.persist_account(self.paper_engine)
        if isinstance(strategy, GridTradingStrategy):
            await self.db.delete_grid_state(strategy.strategy_id)
        elif strategy is None:
            await self.db.delete_grid_state(f"grid:{symbol}")

    def _sync_sma_position(self, symbol: str) -> None:
        """Seed an SMA strategy's position tracker from the engine."""
        strategy = self.strategies.get(symbol)
        if isinstance(strategy, SmaCrossoverStrategy):
            position = self.paper_engine.positions.get(symbol)
            strategy.sync_position(position.amount if position else 0.0)

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------
    async def heartbeat_loop(self) -> None:
        """Publish liveness and last prices for the dashboard."""
        while True:
            try:
                await self.db.set_config_many(
                    {
                        "heartbeat_ts": str(time.time()),
                        "last_prices": json.dumps(self.paper_engine.last_prices()),
                    }
                )
            except Exception as exc:
                logger.error("Heartbeat write failed: {}", exc)
            await asyncio.sleep(self.settings.heartbeat_interval)

    async def equity_snapshot_loop(self) -> None:
        """Periodically append the equity curve to ``equity_history``."""
        while True:
            await asyncio.sleep(self.settings.equity_snapshot_interval)
            try:
                await self.persistence.snapshot_equity(self.paper_engine)
            except Exception as exc:
                logger.error("Equity snapshot failed: {}", exc)

    async def daily_digest_loop(self) -> None:
        """Send a periodic PnL digest to the configured notification channels."""
        baseline = self.paper_engine.equity()
        while True:
            await asyncio.sleep(self.settings.digest_interval_hours * 3600)
            try:
                equity = self.paper_engine.equity()
                delta = equity - baseline
                pct = 100.0 * delta / baseline if baseline else 0.0
                baseline = equity
                positions = sum(
                    1 for p in self.paper_engine.positions.values() if p.amount > 0
                )
                await self.notifier.send(
                    f"📊 PnL digest ({self.settings.digest_interval_hours:.0f}h)\n"
                    f"Equity: {equity:.2f} {self.settings.quote_currency} "
                    f"({delta:+.2f} / {pct:+.2f}%)\n"
                    f"Realized (net): {self.paper_engine.realized_pnl():+.2f} | "
                    f"Unrealized: {self.paper_engine.unrealized_pnl():+.2f}\n"
                    f"Fees paid: {self.paper_engine.total_fees_paid():.2f} | "
                    f"Open orders: {len(self.paper_engine.open_orders)} | "
                    f"Positions: {positions} | "
                    f"Drawdown: {self.risk_manager.drawdown:.2%}"
                )
            except Exception as exc:
                logger.error("Digest failed: {}", exc)

    async def pnl_summary_loop(self) -> None:
        """Periodically log equity, PnL, and open order/position counts."""
        quote = self.settings.quote_currency
        while True:
            await asyncio.sleep(self.settings.pnl_summary_interval)
            equity = self.paper_engine.equity()
            state = "PAUSED" if self._paused else "RUNNING"
            logger.info(
                "PNL [{}] | equity {:.2f} {} | realized {:+.2f} | unrealized {:+.2f} | "
                "fees {:.2f} | drawdown {:.2%} | open orders {} | positions {}",
                state,
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
    # Startup / shutdown
    # ------------------------------------------------------------------
    async def _startup(self) -> None:
        """Connect storage, apply persisted config, and rehydrate state."""
        await self.db.connect()
        await self.config_sync.seed()
        await self.config_sync.set_bot_status("running")

        # Apply dashboard-saved settings from a previous run before building
        # anything that depends on them (symbols, grid parameters, …).
        for change in await self.config_sync.poll():
            logger.info("Persisted setting override: {} = {}", change.key, change.new)

        restored_map = await self.persistence.hydrate_engine(self.paper_engine)
        self._order_strategy.update(restored_map)

        self.strategies = {
            symbol: build_strategy(self.settings, symbol)
            for symbol in self.settings.symbols
        }
        self._aggregators = {
            symbol: CandleAggregator(self.settings.bar_timeframe)
            for symbol in self.strategies
        }
        await self._hydrate_strategies()
        for symbol in self.strategies:
            self._sync_sma_position(symbol)
        self.connector.set_event_handler(self._on_connection_event)

    async def _on_connection_event(self, kind: str, message: str) -> None:
        """Forward connector disconnect/reconnect events to notifications.

        Throttled per event kind so reconnect storms don't spam channels.
        """
        interval = 60.0 if kind == "reconnect" else 300.0
        await self.notifier.send_throttled(f"ws:{kind}", message, interval)

    async def _hydrate_strategies(self) -> None:
        """Restore persisted grid state into freshly built strategies."""
        open_ids = set(self.paper_engine.open_orders)
        for symbol, strategy in self.strategies.items():
            if not isinstance(strategy, GridTradingStrategy):
                continue
            row = await self.db.load_grid_state(strategy.strategy_id)
            if row is None:
                continue
            config = json.loads(row.grid_config_json)
            levels = json.loads(row.active_levels_json)
            if not strategy.matches_config(config):
                logger.warning(
                    "Persisted grid for {} no longer matches configuration — "
                    "canceling its orders and starting a fresh grid",
                    symbol,
                )
                await self._retire_strategy(symbol)
                self.strategies[symbol] = build_strategy(self.settings, symbol)
                continue
            strategy.restore_state(config, levels, open_ids)

    async def _flush_state(self) -> None:
        """Persist every piece of in-memory state (shutdown step 2)."""
        await self.persistence.persist_account(self.paper_engine)
        await self.persistence.persist_orders(
            list(self.paper_engine.open_orders.values()), self._order_strategy
        )
        for strategy in self.strategies.values():
            if isinstance(strategy, GridTradingStrategy):
                await self._persist_grid(strategy)
        await self.persistence.snapshot_equity(self.paper_engine)
        if self.config_sync.bot_status != "stopped":
            await self.config_sync.set_bot_status("stopped")
        logger.info("State flushed to {}", self.db.path)

    async def _cancel_tasks(self) -> None:
        """Stop all streaming and background tasks (shutdown step 1)."""
        self._paused = True  # no new entries while we wind down
        tasks = [*self._ticker_tasks.values(), *self._aux_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._ticker_tasks.clear()
        self._aux_tasks.clear()

    async def run(self) -> None:
        """Start all streams and run until a signal or dashboard stop."""
        await self._startup()

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

        self.lifecycle.install(asyncio.get_running_loop())
        self.lifecycle.add_callback("cancel tasks", self._cancel_tasks)
        self.lifecycle.add_callback("flush state", self._flush_state)
        self.lifecycle.add_callback("close websockets", self.connector.close)
        self.lifecycle.add_callback("close notifier", self.notifier.close)
        self.lifecycle.add_callback("close database", self.db.close)

        self._ticker_tasks = {
            symbol: asyncio.create_task(
                self.connector.watch_ticker(symbol, self.handle_ticker),
                name=f"ticker:{symbol}",
            )
            for symbol in self.settings.symbols
        }
        self._aux_tasks = [
            asyncio.create_task(self.pnl_summary_loop(), name="pnl-summary"),
            asyncio.create_task(self.config_poll_loop(), name="config-poll"),
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self.equity_snapshot_loop(), name="equity-snapshot"),
        ]
        if self.notifier.enabled:
            self._aux_tasks.append(
                asyncio.create_task(self.daily_digest_loop(), name="daily-digest")
            )
            await self.notifier.send(
                f"🚀 Trading bot started | {mode} mode | {self.settings.exchange} | "
                f"{', '.join(self.settings.symbols)} | {self.settings.strategy}"
            )

        try:
            await self.lifecycle.wait()
        finally:
            final_equity = self.paper_engine.equity()
            await self.lifecycle.run_callbacks()
            logger.info(
                "Shutdown complete ({}) | final equity {:.2f} {} | realized PnL {:+.2f}",
                self.lifecycle.reason or "loop exit",
                final_equity,
                self.settings.quote_currency,
                self.paper_engine.realized_pnl(),
            )


def main() -> None:
    """Load settings, configure logging, and run the app until shutdown."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)
    app = TradingApp(settings)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        # Fallback for platforms without loop signal handlers; state was
        # flushed by the lifecycle callbacks inside run()'s finally block.
        logger.info("Interrupted — goodbye.")


if __name__ == "__main__":
    main()
