"""Streamlit web dashboard for the trading bot.

Run alongside the bot process::

    streamlit run dashboard/app.py

The dashboard talks to the bot exclusively through the shared SQLite
database (``DB_PATH``): it reads state tables (positions, orders, equity
history, grid state) with plain ``sqlite3``/``pandas``, and writes runtime
settings and start/pause/stop commands into ``bot_config``, which the bot
polls and applies live. The dashboard never places orders itself.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Make the project root importable when launched as `streamlit run dashboard/app.py`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.config import Settings  # noqa: E402

_STATUS_COLORS = {"RUNNING": "#21c55d", "PAUSED": "#f59e0b", "STOPPED": "#ef4444",
                  "OFFLINE": "#6b7280"}
_CSS = """
<style>
    .block-container { padding-top: 2.4rem; }
    div[data-testid="stMetric"] {
        background: rgba(128, 128, 128, 0.08);
        border: 1px solid rgba(128, 128, 128, 0.18);
        border-radius: 12px;
        padding: 12px 16px;
    }
    .status-badge {
        display: inline-block; padding: 4px 14px; border-radius: 999px;
        color: white; font-weight: 700; letter-spacing: 0.05em;
    }
</style>
"""


# ---------------------------------------------------------------------------
# Database helpers (read-mostly; writes only touch bot_config)
# ---------------------------------------------------------------------------
def get_settings_cached() -> Settings:
    """Process-wide settings (db path, heartbeat interval, log file)."""
    if "settings" not in st.session_state:
        st.session_state["settings"] = Settings()
    return st.session_state["settings"]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings_cached().db_path, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def query_df(sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    """Run one read query and return a DataFrame (empty on missing DB)."""
    try:
        with _connect() as conn:
            return pd.read_sql_query(sql, conn, params=params)
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return pd.DataFrame()


def load_config() -> dict[str, str]:
    """Read the full ``bot_config`` table."""
    df = query_df("SELECT key, value FROM bot_config")
    return dict(zip(df["key"], df["value"])) if not df.empty else {}


def save_config(values: dict[str, str]) -> None:
    """Upsert ``bot_config`` entries (picked up by the bot's config poll)."""
    now = time.time()
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO bot_config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            [(k, v, now) for k, v in values.items()],
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------
def bot_display_status(cfg: dict[str, str]) -> str:
    """RUNNING / PAUSED / STOPPED from bot_config, or OFFLINE if the
    heartbeat is stale (bot process not running)."""
    heartbeat = float(cfg.get("heartbeat_ts", 0) or 0)
    stale_after = 3 * get_settings_cached().heartbeat_interval
    if time.time() - heartbeat > stale_after:
        return "OFFLINE"
    return cfg.get("bot_status", "stopped").upper()


def last_prices(cfg: dict[str, str]) -> dict[str, float]:
    """Latest per-symbol prices published by the bot's heartbeat."""
    try:
        return {k: float(v) for k, v in json.loads(cfg.get("last_prices", "{}")).items()}
    except (ValueError, TypeError):
        return {}


def equity_metrics() -> tuple[float | None, float | None, float | None, float | None]:
    """Return (equity, pnl_24h_abs, pnl_24h_pct, realized_pnl) or Nones."""
    latest = query_df(
        "SELECT total_equity, realized_pnl FROM equity_history "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    if latest.empty:
        return None, None, None, None
    equity = float(latest["total_equity"].iloc[0])
    realized = float(latest["realized_pnl"].iloc[0])
    day_ago = time.time() - 86_400
    ref = query_df(
        "SELECT total_equity FROM equity_history WHERE timestamp >= ? "
        "ORDER BY timestamp ASC LIMIT 1",
        (day_ago,),
    )
    if ref.empty:
        return equity, None, None, realized
    base = float(ref["total_equity"].iloc[0])
    delta = equity - base
    pct = (delta / base * 100.0) if base else None
    return equity, delta, pct, realized


# ---------------------------------------------------------------------------
# Page sections
# ---------------------------------------------------------------------------
def render_header(cfg: dict[str, str]) -> None:
    """Status badge, start/pause/stop controls, and the key metrics row."""
    status = bot_display_status(cfg)
    color = _STATUS_COLORS.get(status, "#6b7280")

    left, b1, b2, b3 = st.columns([3, 1, 1, 1], vertical_alignment="center")
    with left:
        st.markdown(
            f"## 🤖 Trading Bot &nbsp; "
            f"<span class='status-badge' style='background:{color}'>{status}</span>",
            unsafe_allow_html=True,
        )
        if status == "OFFLINE":
            st.caption("Bot process is not running — start it with `python main.py`.")
    if b1.button("▶ Start", width="stretch", type="primary"):
        save_config({"bot_status": "running"})
        st.rerun()
    if b2.button("⏸ Pause", width="stretch"):
        save_config({"bot_status": "paused"})
        st.rerun()
    if b3.button("⏹ Stop", width="stretch"):
        save_config({"bot_status": "stopped"})
        st.rerun()

    equity, pnl24, pnl24_pct, realized = equity_metrics()
    positions = query_df("SELECT * FROM positions WHERE amount > 0")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Equity", f"€{equity:,.2f}" if equity is not None else "—")
    m2.metric(
        "24h PnL",
        f"€{pnl24:+,.2f}" if pnl24 is not None else "—",
        f"{pnl24_pct:+.2f}%" if pnl24_pct is not None else None,
    )
    m3.metric("Realized PnL", f"€{realized:+,.2f}" if realized is not None else "—")
    m4.metric("Open Positions", len(positions))


def render_settings_tab(cfg: dict[str, str]) -> None:
    """Runtime-adjustable settings persisted to ``bot_config``."""
    st.subheader("Control & Settings")
    st.caption(
        "Saved values are written to the `bot_config` table and applied by "
        "the running bot within its poll interval — no restart needed "
        "(except for the exchange)."
    )

    def fnum(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except ValueError:
            return default

    with st.form("settings_form"):
        c1, c2 = st.columns(2)
        with c1:
            symbols = st.text_input(
                "Trading pairs (comma-separated)",
                value=cfg.get("symbols", "BTC/EUR"),
                help="e.g. BTC/EUR or BTC/EUR,ETH/EUR",
            )
            exchanges = ["bitvavo", "kraken"]
            exchange = st.selectbox(
                "Exchange (applies after bot restart)",
                exchanges,
                index=exchanges.index(cfg.get("exchange", "bitvavo"))
                if cfg.get("exchange", "bitvavo") in exchanges else 0,
            )
            grid_levels = st.number_input(
                "Grid levels", min_value=1, max_value=50,
                value=int(fnum("grid_levels", 5)),
            )
            grid_spacing = st.number_input(
                "Grid spacing (%)", min_value=0.01, max_value=49.0,
                value=fnum("grid_spacing_pct", 0.005) * 100.0,
                step=0.05, format="%.2f",
            )
        with c2:
            stop_loss = st.number_input(
                "Stop-loss (%)", min_value=0.1, max_value=99.0,
                value=fnum("stop_loss_pct", 0.02) * 100.0, step=0.1, format="%.1f",
            )
            take_profit = st.number_input(
                "Take-profit (%)", min_value=0.1, max_value=99.0,
                value=fnum("take_profit_pct", 0.04) * 100.0, step=0.1, format="%.1f",
            )
            max_alloc = st.number_input(
                "Max allocation per trade (%)", min_value=1.0, max_value=100.0,
                value=fnum("max_allocation_pct", 0.10) * 100.0, step=1.0, format="%.0f",
            )
            order_size = st.number_input(
                "Grid order size (€)", min_value=1.0,
                value=fnum("grid_order_quote_size", 100.0), step=10.0,
            )

        if st.form_submit_button("💾 Save & Apply Settings", type="primary"):
            # Fee floor: a grid cycle earns the spacing and pays ~2 maker
            # fees; spacing below that is a guaranteed structural loss.
            fee_floor_pct = (
                2.0 * get_settings_cached().maker_fee_rate + 0.001
            ) * 100.0
            if grid_spacing < fee_floor_pct:
                st.error(
                    f"Not saved: grid spacing {grid_spacing:.2f}% is below the "
                    f"fee floor {fee_floor_pct:.2f}% (2 × maker fee + 0.1% "
                    f"margin) — every grid cycle would lose money."
                )
            else:
                save_config(
                    {
                        "symbols": ",".join(
                            s.strip().upper() for s in symbols.split(",") if s.strip()
                        ),
                        "exchange": exchange,
                        "grid_levels": str(int(grid_levels)),
                        "grid_spacing_pct": str(grid_spacing / 100.0),
                        "grid_order_quote_size": str(order_size),
                        "stop_loss_pct": str(stop_loss / 100.0),
                        "take_profit_pct": str(take_profit / 100.0),
                        "max_allocation_pct": str(max_alloc / 100.0),
                    }
                )
                st.success("Settings saved — the bot applies them on its next poll.")

    st.divider()
    st.markdown("#### 📣 Notifications")
    from utils.notifications import Notifier

    notifier_settings = get_settings_cached()
    channels = Notifier.from_settings(notifier_settings).channels
    if channels:
        st.caption(f"Configured channels: {', '.join(channels)}")
    else:
        st.caption(
            "No channels configured — set `TELEGRAM_BOT_TOKEN` + "
            "`TELEGRAM_CHAT_ID` and/or `DISCORD_WEBHOOK_URL` in `.env`."
        )
    if st.button("🔔 Send test notification"):
        if not channels:
            st.warning("Configure at least one webhook channel in `.env` first.")
        else:
            import asyncio

            async def _send_test() -> bool:
                notifier = Notifier.from_settings(notifier_settings)
                try:
                    return await notifier.send(
                        "🔔 Test notification from the trading bot dashboard"
                    )
                finally:
                    await notifier.close()

            if asyncio.run(_send_test()):
                st.success(f"Test message delivered via {', '.join(channels)}.")
            else:
                st.error("Delivery failed — check tokens/URLs and the bot log.")


def render_portfolio_tab(cfg: dict[str, str]) -> None:
    """Open positions, resting orders, and recent fill history."""
    st.subheader("Open Positions")
    positions = query_df("SELECT * FROM positions WHERE amount > 0")
    if positions.empty:
        st.info("No open positions.")
    else:
        prices = last_prices(cfg)
        positions["current_price"] = positions["symbol"].map(prices)
        positions["unrealized_pnl"] = (
            (positions["current_price"] - positions["entry_price"]) * positions["amount"]
        )
        st.dataframe(
            positions[
                ["symbol", "amount", "entry_price", "current_price",
                 "unrealized_pnl", "realized_pnl", "stop_loss", "take_profit"]
            ].round(6),
            width="stretch", hide_index=True,
        )

    st.subheader("Active Orders")
    open_orders = query_df(
        "SELECT id, symbol, side, type, price, amount, strategy_id, created_at "
        "FROM orders WHERE status = 'open' ORDER BY price DESC"
    )
    if open_orders.empty:
        st.info("No resting orders.")
    else:
        open_orders["created_at"] = pd.to_datetime(
            open_orders["created_at"], unit="s"
        ).dt.strftime("%Y-%m-%d %H:%M:%S")
        st.dataframe(open_orders, width="stretch", hide_index=True)

    st.subheader("Recent Fills")
    fills = query_df(
        "SELECT symbol, side, type, average_price AS fill_price, filled, fee, "
        "strategy_id, updated_at FROM orders WHERE status = 'filled' "
        "ORDER BY updated_at DESC LIMIT 50"
    )
    if fills.empty:
        st.info("No fills yet.")
    else:
        fills["updated_at"] = pd.to_datetime(
            fills["updated_at"], unit="s"
        ).dt.strftime("%Y-%m-%d %H:%M:%S")
        st.dataframe(fills, width="stretch", hide_index=True)


def render_analytics_tab(cfg: dict[str, str]) -> None:
    """Equity curve and grid level visualization."""
    st.subheader("Equity Over Time")
    history = query_df(
        "SELECT timestamp, total_equity, realized_pnl, unrealized_pnl "
        "FROM equity_history ORDER BY timestamp ASC LIMIT 5000"
    )
    if history.empty:
        st.info("No equity history recorded yet — start the bot to collect data.")
    else:
        history["time"] = pd.to_datetime(history["timestamp"], unit="s")
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=history["time"], y=history["total_equity"],
                mode="lines", name="Equity", fill="tozeroy",
                line={"color": "#6366f1", "width": 2},
            )
        )
        fig.update_layout(
            height=340, margin={"l": 10, "r": 10, "t": 10, "b": 10},
            yaxis_title="Equity (€)",
        )
        fig.update_yaxes(rangemode="normal")
        st.plotly_chart(fig, width="stretch")

    st.subheader("Grid Levels")
    grids = query_df("SELECT * FROM grid_state")
    if grids.empty:
        st.info("No active grid state.")
        return
    prices = last_prices(cfg)
    for _, row in grids.iterrows():
        symbol = str(row["symbol"])
        config = json.loads(row["grid_config_json"])
        levels = json.loads(row["active_levels_json"])
        current = prices.get(symbol)

        st.markdown(
            f"**{symbol}** — anchor €{config.get('anchor_price') or 0:,.2f}, "
            f"{config.get('total_cycles', 0)} cycles completed"
        )
        fig = go.Figure()
        for level in levels:
            state = level.get("state", "IDLE")
            buy_price = float(level["buy_price"])
            sell_price = float(level["sell_price"])
            if state == "BUY_PENDING":
                fig.add_hline(y=buy_price, line_color="#21c55d", line_dash="dash",
                              annotation_text=f"buy €{buy_price:,.2f}")
            elif state == "SELL_PENDING":
                fig.add_hline(y=sell_price, line_color="#ef4444", line_dash="dash",
                              annotation_text=f"sell €{sell_price:,.2f}")
            else:
                fig.add_hline(y=buy_price, line_color="#9ca3af", line_dash="dot",
                              opacity=0.5)
        if current:
            fig.add_hline(y=current, line_color="#6366f1", line_width=3,
                          annotation_text=f"price €{current:,.2f}")
        fig.update_layout(
            height=320, margin={"l": 10, "r": 10, "t": 10, "b": 10},
            yaxis_title="Price (€)", xaxis={"visible": False},
        )
        st.plotly_chart(fig, width="stretch")


def render_backtest_tab(cfg: dict[str, str]) -> None:
    """Historical backtesting: control form, metrics, and Plotly analysis."""
    from dataclasses import asdict
    from datetime import date, datetime, timedelta, timezone

    st.subheader("Backtesting")
    st.caption(
        "Downloads historical OHLCV data via CCXT (cached locally in "
        "`data/historical/`) and replays it through the same strategy, "
        "matching, and risk logic the live bot uses."
    )

    def fnum(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except ValueError:
            return default

    symbols = [s.strip() for s in cfg.get("symbols", "BTC/EUR").split(",") if s.strip()]
    symbol_options = list(dict.fromkeys(symbols + ["BTC/EUR", "ETH/EUR", "XRP/EUR", "SOL/EUR"]))

    with st.form("backtest_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            symbol = st.selectbox("Symbol", symbol_options, index=0)
            timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "1h", "1d"], index=3)
            exchange = st.selectbox("Exchange", ["bitvavo", "kraken"], index=0)
            capital = st.number_input(
                "Initial capital (€)", min_value=100.0, value=10_000.0, step=500.0
            )
        with c2:
            start_date = st.date_input("Start date", value=date.today() - timedelta(days=30))
            end_date = st.date_input("End date", value=date.today())
            strategy_name = st.selectbox("Strategy", ["grid", "sma_crossover"], index=0)
            st.markdown("**Grid parameters**")
            grid_levels = st.number_input("Grid levels", 1, 50, int(fnum("grid_levels", 5)))
            grid_spacing = st.number_input(
                "Grid spacing (%)", 0.01, 49.0,
                fnum("grid_spacing_pct", 0.005) * 100.0, step=0.05, format="%.2f",
            )
            grid_order_size = st.number_input(
                "Grid order size (€)", 1.0, value=fnum("grid_order_quote_size", 100.0)
            )
            grid_aligned_sl = st.checkbox(
                "Grid-bewuste stop-loss (SL onder de hele grid)", value=True,
                help="Uit = klassieke stop-loss per aankoop",
            )
            grid_regime = st.checkbox(
                "Regime-filter (pauzeer kopen in dalende trend)", value=True,
            )
        with c3:
            st.markdown("**SMA parameters**")
            sma_fast = st.number_input("SMA fast period", 2, 500, 10)
            sma_slow = st.number_input("SMA slow period", 3, 1000, 30)
            st.markdown("**Costs**")
            maker_fee = st.slider("Maker fee (%)", 0.0, 1.0, 0.15, step=0.01)
            taker_fee = st.slider("Taker fee (%)", 0.0, 1.0, 0.25, step=0.01)
            slippage = st.slider("Slippage (%)", 0.0, 1.0, 0.05, step=0.01)

        run_clicked = st.form_submit_button("🚀 Run Backtest", type="primary")

    if run_clicked:
        import asyncio

        from backtester.data_loader import OHLCVDataLoader, candles_from_df
        from backtester.engine import BacktestEngine
        from backtester.metrics import compute_metrics
        from strategies.grid_trading import GridTradingStrategy
        from strategies.sma_crossover import SmaCrossoverStrategy

        if sma_slow <= sma_fast:
            st.error("SMA slow period must be greater than the fast period.")
            return
        if end_date <= start_date:
            st.error("End date must be after the start date.")
            return
        bt_fee_floor = 2.0 * maker_fee + 0.1  # both in percent
        if strategy_name == "grid" and grid_spacing < bt_fee_floor:
            st.warning(
                f"Grid spacing {grid_spacing:.2f}% is below the fee floor "
                f"{bt_fee_floor:.2f}% (2 × maker fee + 0.1%) — expect every "
                f"cycle to lose money in this backtest."
            )
        start_ms = int(datetime.combine(start_date, datetime.min.time(), timezone.utc).timestamp() * 1000)
        end_ms = int(datetime.combine(end_date, datetime.max.time(), timezone.utc).timestamp() * 1000)

        try:
            with st.spinner("Loading historical data (cache-aware)…"):
                loader = OHLCVDataLoader(exchange_id=exchange)
                df = loader.load(symbol, timeframe, start_ms, end_ms)
            if df.empty:
                st.error("No historical data available for this range.")
                return
            if strategy_name == "grid":
                from strategies.regime import RegimeFilter

                strategy = GridTradingStrategy(
                    symbol, levels=int(grid_levels),
                    spacing_pct=grid_spacing / 100.0,
                    order_quote_size=grid_order_size,
                    aligned_protection=grid_aligned_sl,
                    stop_loss_buffer_pct=fnum("stop_loss_pct", 0.02),
                    take_profit_buffer_pct=fnum("take_profit_pct", 0.04),
                    regime_filter=RegimeFilter() if grid_regime else None,
                )
            else:
                strategy = SmaCrossoverStrategy(
                    symbol, fast_period=int(sma_fast), slow_period=int(sma_slow),
                    order_quote_size=grid_order_size,
                )
            settings = Settings(
                _env_file=None,
                symbols=[symbol],
                stop_loss_pct=fnum("stop_loss_pct", 0.02),
                take_profit_pct=fnum("take_profit_pct", 0.04),
                max_allocation_pct=fnum("max_allocation_pct", 0.10),
            )
            engine = BacktestEngine(
                strategy=strategy, settings=settings, initial_capital=capital,
                maker_fee_rate=maker_fee / 100.0, taker_fee_rate=taker_fee / 100.0,
                slippage_rate=slippage / 100.0,
            )
            with st.spinner(f"Replaying {len(df)} candles…"):
                result = asyncio.run(engine.run(candles_from_df(df), timeframe=timeframe))
            st.session_state["backtest"] = {
                "result": result,
                "metrics": compute_metrics(result),
                "candles": df,
            }
        except Exception as exc:  # surface loader/engine errors in the UI
            st.error(f"Backtest failed: {exc}")
            return

    stored = st.session_state.get("backtest")
    if not stored:
        st.info("Configure a backtest above and press **Run Backtest**.")
        return

    from backtester.models import drawdown_series

    result = stored["result"]
    metrics = stored["metrics"]
    candles = stored["candles"]

    # --- metric cards ---------------------------------------------------
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric(
        "Bot Return", f"{metrics.total_return_pct:+.2f}%",
        f"{metrics.total_return_pct - metrics.benchmark_return_pct:+.2f}% vs B&H",
    )
    m2.metric("Buy & Hold", f"{metrics.benchmark_return_pct:+.2f}%")
    m3.metric("Max Drawdown", f"−{metrics.max_drawdown_pct:.2f}%")
    m4.metric("Sharpe", f"{metrics.sharpe_ratio:.2f}")
    m5.metric(
        "Win Rate", f"{metrics.win_rate_pct:.1f}%",
        f"{metrics.winning_trades}W / {metrics.losing_trades}L",
    )
    st.caption(
        f"PnL €{metrics.total_pnl:+,.2f} · CAGR {metrics.cagr_pct:+.2f}% · "
        f"Sortino {metrics.sortino_ratio:.2f} · profit factor "
        f"{metrics.profit_factor:.2f} · {metrics.total_trades} trades · "
        f"fees €{metrics.total_fees:,.2f}"
    )

    times = pd.to_datetime(pd.Series(result.timestamps), unit="s")

    # --- chart 1: equity vs buy & hold ----------------------------------
    st.markdown("##### Equity curve vs Buy & Hold")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=times, y=result.equity, name="Strategy",
                             line={"color": "#6366f1", "width": 2}))
    fig.add_trace(go.Scatter(x=times, y=result.benchmark, name="Buy & Hold",
                             line={"color": "#9ca3af", "width": 2, "dash": "dot"}))
    fig.update_layout(height=340, margin={"l": 10, "r": 10, "t": 10, "b": 10},
                      yaxis_title="Equity (€)", legend={"orientation": "h"})
    st.plotly_chart(fig, width="stretch")

    # --- chart 2: price with trade markers ------------------------------
    st.markdown("##### Price & trades")
    fig = go.Figure()
    if len(candles) <= 2000:
        fig.add_trace(
            go.Candlestick(
                x=pd.to_datetime(candles["timestamp"], unit="ms"),
                open=candles["open"], high=candles["high"],
                low=candles["low"], close=candles["close"],
                name="Price", showlegend=False,
            )
        )
        fig.update_layout(xaxis_rangeslider_visible=False)
    else:
        fig.add_trace(go.Scatter(x=times, y=result.close_prices, name="Close",
                                 line={"color": "#9ca3af", "width": 1.5}))
    buys = [t for t in result.trades if t.side == "buy"]
    sells = [t for t in result.trades if t.side == "sell"]
    if buys:
        fig.add_trace(go.Scatter(
            x=pd.to_datetime([t.timestamp for t in buys], unit="s"),
            y=[t.price for t in buys], mode="markers", name="Entries",
            marker={"symbol": "triangle-up", "size": 10, "color": "#21c55d"},
        ))
    if sells:
        fig.add_trace(go.Scatter(
            x=pd.to_datetime([t.timestamp for t in sells], unit="s"),
            y=[t.price for t in sells], mode="markers", name="Exits",
            marker={"symbol": "triangle-down", "size": 10, "color": "#ef4444"},
        ))
    fig.update_layout(height=380, margin={"l": 10, "r": 10, "t": 10, "b": 10},
                      yaxis_title="Price (€)", legend={"orientation": "h"})
    st.plotly_chart(fig, width="stretch")

    # --- chart 3: drawdown ----------------------------------------------
    st.markdown("##### Drawdown")
    dd = [100.0 * d for d in drawdown_series(result.equity)]
    fig = go.Figure(
        go.Scatter(x=times, y=dd, fill="tozeroy", name="Drawdown",
                   line={"color": "#ef4444", "width": 1.5})
    )
    fig.update_layout(height=240, margin={"l": 10, "r": 10, "t": 10, "b": 10},
                      yaxis_title="Drawdown (%)")
    st.plotly_chart(fig, width="stretch")

    # --- trade history ---------------------------------------------------
    st.markdown("##### Trade history")
    if not result.trades:
        st.info("No trades were executed in this backtest.")
        return
    trades_df = pd.DataFrame([asdict(t) for t in result.trades])
    trades_df["time"] = pd.to_datetime(trades_df["timestamp"], unit="s")
    trades_df = trades_df[
        ["time", "side", "type", "price", "amount", "fee", "realized_pnl"]
    ]
    numeric_cols = ["price", "amount", "fee", "realized_pnl"]
    trades_df[numeric_cols] = trades_df[numeric_cols].round(6)
    st.dataframe(trades_df, width="stretch", hide_index=True)
    st.download_button(
        "⬇️ Download trades CSV",
        data=trades_df.to_csv(index=False).encode("utf-8"),
        file_name=f"backtest_{result.symbol.replace('/', '-')}_{result.timeframe}.csv",
        mime="text/csv",
    )


def render_optimizer_tab(cfg: dict[str, str]) -> None:
    """Grid parameter sweep with in-sample / out-of-sample validation."""
    st.subheader("Parameter Sweep (grid)")
    st.caption(
        "Test tientallen combinaties van spacing × levels × stop-loss in één "
        "run. Elke combinatie draait drie keer: de hele periode, een "
        "trainvenster (eerste 70%) en een **out-of-sample** testvenster "
        "(laatste 30%). Alleen combinaties die in *beide* vensters netto "
        "winnen zijn serieuze kandidaten — de rest is meestal toeval."
    )

    symbols = [s.strip() for s in cfg.get("symbols", "BTC/EUR").split(",") if s.strip()]
    symbol_options = list(dict.fromkeys(symbols + ["BTC/EUR", "ETH/EUR", "XRP/EUR", "SOL/EUR"]))

    with st.form("sweep_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            symbol = st.selectbox("Symbol", symbol_options, index=0)
            timeframe = st.selectbox("Timeframe", ["15m", "1h", "4h", "1d"], index=1)
            days = st.number_input("Periode (dagen)", 14, 365, 90)
            exchange = st.selectbox("Exchange", ["bitvavo", "kraken"], index=0)
        with c2:
            capital = st.number_input("Startkapitaal (€)", 100.0, value=10_000.0, step=500.0)
            order_size = st.number_input("Grid order size (€)", 1.0, value=100.0, step=10.0)
            aligned = st.checkbox(
                "Grid-bewuste stop-loss (SL onder de hele grid)", value=True,
                help="Uit = klassieke stop-loss per aankoop (ter vergelijking)",
            )
            regime = st.checkbox(
                "Regime-filter (pauzeer kopen in dalende trend)", value=True,
            )
        with c3:
            maker_fee = st.slider("Maker fee (%)", 0.0, 1.0, 0.15, step=0.01)
            taker_fee = st.slider("Taker fee (%)", 0.0, 1.0, 0.25, step=0.01)
            slippage = st.slider("Slippage (%)", 0.0, 1.0, 0.05, step=0.01)

        spacings = st.multiselect(
            "Grid spacing (%)", [0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0],
            default=[0.5, 1.0, 1.5, 2.0, 3.0],
        )
        levels_options = st.multiselect("Grid levels", [3, 5, 8, 12], default=[3, 5, 8])
        stop_losses = st.multiselect(
            "Stop-loss (%)", [2.0, 5.0, 10.0, 15.0, 25.0], default=[2.0, 10.0],
            help="Bij grid-bewuste SL: buffer onder de onderkant van de grid",
        )
        run_sweep_clicked = st.form_submit_button("🔬 Run Sweep", type="primary")

    if run_sweep_clicked:
        import asyncio

        from backtester.data_loader import OHLCVDataLoader, candles_from_df
        from backtester.optimizer import sweep_grid

        if not spacings or not levels_options or not stop_losses:
            st.error("Kies minstens één waarde per parameter.")
            return
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - int(days) * 86_400_000
        try:
            with st.spinner("Historische data laden (cache-aware)…"):
                loader = OHLCVDataLoader(exchange_id=exchange)
                df = loader.load(symbol, timeframe, start_ms, end_ms)
            if df.empty:
                st.error("Geen historische data voor deze periode.")
                return
            candles = candles_from_df(df)
            bar = st.progress(0.0, text="Sweep draait…")

            def on_progress(done: int, total: int) -> None:
                bar.progress(
                    done / total, text=f"Sweep draait… {done}/{total} combinaties"
                )

            results = asyncio.run(
                sweep_grid(
                    candles, symbol, timeframe,
                    spacings=[s / 100.0 for s in spacings],
                    levels_options=[int(v) for v in levels_options],
                    stop_loss_pcts=[s / 100.0 for s in stop_losses],
                    order_quote_size=order_size,
                    initial_capital=capital,
                    maker_fee_rate=maker_fee / 100.0,
                    taker_fee_rate=taker_fee / 100.0,
                    slippage_rate=slippage / 100.0,
                    aligned_protection=aligned,
                    regime_filter=regime,
                    progress=on_progress,
                )
            )
            bar.empty()
            st.session_state["sweep"] = {
                "results": results, "symbol": symbol, "timeframe": timeframe,
                "days": days,
            }
        except Exception as exc:
            st.error(f"Sweep mislukt: {exc}")
            return

    stored = st.session_state.get("sweep")
    if not stored:
        st.info("Stel de parameterlijsten in en klik **Run Sweep**.")
        return

    results = stored["results"]
    viable = [r for r in results if r.viable]
    robust = [r for r in viable if r.robust]
    excluded = [r for r in results if not r.viable]

    if robust:
        best = robust[0]
        st.success(
            f"**{len(robust)} van {len(viable)} combinaties zijn robuust** "
            f"(winst in train én test). Beste out-of-sample: spacing "
            f"{best.spacing_pct:.2%}, {best.levels} levels, SL "
            f"{best.stop_loss_pct:.0%} → test {best.test_return_pct:+.2f}% "
            f"(B&H hele periode: {best.benchmark_return_pct:+.2f}%)"
        )
    else:
        st.warning(
            "**Geen enkele combinatie is winstgevend in zowel train- als "
            "testperiode.** Dat is een geldig resultaat: in deze periode "
            "heeft de grid-strategie op dit symbool geen netto voordeel. "
            "Probeer een andere periode/timeframe — en als dat beeld blijft, "
            "is niet-live-gaan de winstgevende keuze."
        )
    if excluded:
        st.caption(
            f"{len(excluded)} combinaties overgeslagen: spacing onder de "
            f"fee-vloer (verliezen per constructie)."
        )

    # --- results table ---------------------------------------------------
    table = pd.DataFrame([r.to_dict() for r in viable])
    if not table.empty:
        table = table[
            ["robust", "spacing_pct", "levels", "stop_loss_pct",
             "train_return_pct", "test_return_pct", "full_return_pct",
             "benchmark_return_pct", "max_drawdown_pct", "win_rate_pct",
             "profit_factor", "total_trades", "total_fees"]
        ]
        table["spacing_pct"] = (table["spacing_pct"] * 100).round(2)
        table["stop_loss_pct"] = (table["stop_loss_pct"] * 100).round(1)
        numeric = ["train_return_pct", "test_return_pct", "full_return_pct",
                   "benchmark_return_pct", "max_drawdown_pct", "win_rate_pct",
                   "profit_factor", "total_fees"]
        table[numeric] = table[numeric].round(2)
        table = table.rename(columns={
            "robust": "✓", "spacing_pct": "spacing %", "stop_loss_pct": "SL %",
            "train_return_pct": "train %", "test_return_pct": "test %",
            "full_return_pct": "totaal %", "benchmark_return_pct": "B&H %",
            "max_drawdown_pct": "maxDD %", "win_rate_pct": "winrate %",
            "profit_factor": "PF", "total_trades": "trades",
            "total_fees": "fees €",
        })
        st.dataframe(table, width="stretch", hide_index=True)

    # --- heatmap ---------------------------------------------------------
    level_values = sorted({r.levels for r in viable})
    if level_values:
        chosen_levels = st.radio(
            "Heatmap voor grid levels:", level_values, horizontal=True
        )
        subset = [r for r in viable if r.levels == chosen_levels]
        xs = sorted({r.stop_loss_pct for r in subset})
        ys = sorted({r.spacing_pct for r in subset})
        z = [
            [
                next(
                    (r.full_return_pct for r in subset
                     if r.spacing_pct == y_val and r.stop_loss_pct == x_val),
                    None,
                )
                for x_val in xs
            ]
            for y_val in ys
        ]
        fig = go.Figure(
            go.Heatmap(
                z=z,
                x=[f"SL {x:.0%}" for x in xs],
                y=[f"{y:.2%}" for y in ys],
                colorscale="RdYlGn", zmid=0,
                texttemplate="%{z:.2f}%",
                colorbar={"title": "Netto rendement %"},
            )
        )
        fig.update_layout(
            height=90 + 60 * len(ys),
            margin={"l": 10, "r": 10, "t": 30, "b": 10},
            title=f"Netto rendement hele periode — {chosen_levels} levels",
            yaxis_title="Grid spacing",
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "Let op: kies op basis van de **test %**-kolom (out-of-sample), "
            "niet op de mooiste cel in deze heatmap — die kan overfitting zijn."
        )


def render_help_tab() -> None:
    """In-app handleiding: keys, live gaan, exchanges, meldingen, FAQ."""
    st.subheader("Help & Handleiding")
    st.caption(
        "Alles wat je moet weten om deze bot te bedienen. Instellingen met "
        "een 🔐 zet je als environment variable op de server (Render → je "
        "service → Environment → Edit), nooit in de code."
    )

    with st.expander("📖 Wat is grid trading? En SMA? (begrippen uitgelegd)"):
        st.markdown(
            """
**Grid trading** — je legt een "rooster" (grid) van kooporders onder de
huidige prijs, bijvoorbeeld elke 1% één. Zakt de prijs 1%, dan koop je een
klein beetje; stijgt hij daarna weer 1%, dan verkoop je dat plukje met
winst. Je verdient dus aan het **op-en-neer wiebelen** van de prijs, niet
aan de richting. Werkt goed in zijwaartse (wiebelende) markten; verliest
in sterke dalingen (je koopt steeds bij terwijl het blijft zakken — daarom
zit er nu een regime-filter en een noodstop onder de hele grid) en
verdient weinig in sterke stijgingen (je verkoopt te vroeg).

**SMA (Simple Moving Average)** — het gemiddelde van de laatste N
slotkoersen, bijvoorbeeld de laatste 30 uren. Het "gladt" de prijs af
zodat je de onderliggende richting ziet.

**SMA-crossover-strategie** — gebruikt twee gemiddelden: een snel (bijv.
10 bars) en een traag (bijv. 30 bars). Kruist het snelle gemiddelde
**omhoog** door het trage ("golden cross"), dan is de trend opwaarts →
kopen. Kruist het **omlaag** ("death cross") → verkopen. Dit is een
**trendvolgende** strategie: goed in lange stijgingen, maar in wiebelende
markten geeft hij veel valse signalen (en elke valse ronde kost fees).

**Vuistregel:** grid = verdient aan wiebelen, SMA = verdient aan trends.
Ze zijn elkaars spiegelbeeld — vandaar dat de Optimizer je laat zien in
welk regime je data zat.

**Andere termen die je tegenkomt:**
- *Maker/taker fee* — kosten per transactie: maker (order die wacht in
  het orderboek, 0,15%) is goedkoper dan taker (order die direct
  uitgevoerd wordt, 0,25%).
- *Slippage* — het prijsverschil tussen wat je wilde en wat je kreeg bij
  een directe (market) order.
- *Stop-loss / take-profit* — automatische verkoop bij een bepaald
  verlies (noodrem) of winst (winst veiligstellen).
- *Drawdown* — hoe diep je portefeuille onder zijn hoogste punt zakt.
- *Sharpe-ratio* — rendement gedeeld door beweeglijkheid; hoger = meer
  rendement per eenheid risico (>1 is netjes).
- *Buy & Hold (B&H)* — gewoon kopen en vasthouden; de lat waar elke
  strategie overheen moet om zinvol te zijn.
            """
        )

    with st.expander("🚀 Hoe werkt deze bot?", expanded=True):
        st.markdown(
            """
- De bot draait standaard in **paper-modus**: hij handelt met nepgeld
  (startsaldo €10.000) tegen **echte live marktprijzen** van de exchange.
  Er wordt niets gekocht of verkocht op je echte account.
- De **grid-strategie** zet kooporders onder de huidige prijs. Zakt de
  prijs op zo'n niveau, dan koopt hij; stijgt de prijs daarna één niveau,
  dan verkoopt hij met winst. De stop-loss zit standaard **onder de
  onderkant van de hele grid** (niet onder elke losse aankoop — dat zou de
  dips die de grid juist wil kopen omzetten in verliezen) en er is een
  take-profit boven het anker.
- Gebruik het **🔬 Optimizer**-tabblad om tientallen
  instelling-combinaties tegelijk te backtesten; alleen combinaties die
  in trainings- én testperiode winnen zijn serieuze kandidaten.
- Alles wat je hier in **Control & Settings** opslaat past de bot binnen
  enkele seconden toe — geen herstart nodig.
- **Pause** = geen nieuwe aankopen (verkopen en beveiligingen blijven
  actief). **Stop** = proces stoppen; op Render start de service daarna
  automatisch opnieuw, gebruik dus meestal Pause.
- Test een strategie-idee altijd eerst in het **Backtesting**-tabblad
  voordat je de live instellingen aanpast.
            """
        )

    with st.expander("🔑 Bitvavo / Kraken API-keys instellen"):
        st.markdown(
            """
API-keys zijn **alleen nodig om met echt geld te handelen**. Paper trading
werkt zonder keys — marktdata is publiek.

**Bitvavo:**
1. Log in op bitvavo.com → profiel-icoon → **API**
2. Maak een nieuwe API-key aan met rechten **Bekijken** en **Handelen**
3. ⛔ Zet **Opnames/Withdrawals UIT** — de bot hoeft nooit geld op te
   nemen, en een gelekte key kan dan geen geld wegsluizen
4. Vul de key en secret in als 🔐 `API_KEY` en 🔐 `API_SECRET`

**Kraken:** kraken.com → Settings → **API** → Create key, met permissies
*Query Funds* en *Create & Modify Orders* (geen withdrawal-rechten).
Zet daarnaast 🔐 `EXCHANGE` op `kraken` (vereist een herstart van de bot).

**Waar invullen (Render):** je service → **Environment** → knop
**Edit** → voeg de variabele toe (staat hij niet in de lijst, dan maak je
hem daar gewoon nieuw aan) → **Save**. De service herstart vanzelf en de
bot gaat verder waar hij was.
            """
        )

    with st.expander("⚠️ Live gaan met echt geld — lees dit eerst"):
        st.markdown(
            """
**Veiligheidsmodel:** echte orders zijn hard geblokkeerd zolang
`PAPER_TRADING=True` staat. Live handelen vereist twee bewuste stappen —
er is geen knop in dit dashboard die dat per ongeluk kan aanzetten.

**Stappenplan:**
1. Draai **minimaal een paar weken** paper en controleer onder Analytics
   dat de bot *netto* (na fees) structureel iets verdient en zich
   verstandig gedraagt in dalingen
2. Maak API-keys aan zoals hierboven (zonder withdrawal-rechten!)
3. Zet in Render → Environment: 🔐 `API_KEY`, 🔐 `API_SECRET` en wijzig
   🔐 `PAPER_TRADING` naar `False`
4. De service herstart; in de logs zie je de waarschuwing
   **"LIVE TRADING ENABLED"**
5. **Begin klein**: verlaag eerst `Grid order size` (bijv. €10–25) en
   houd de eerste dagen de meldingen en het dashboard goed in de gaten

**Eerlijke waarschuwing:** de paper-modus van deze bot is grondig getest;
de live-modus is functioneel maar eenvoudiger uitgevoerd (orders worden
geplaatst, maar fill-tracking via de exchange is basaal en stop-losses
worden door de bot zelf bewaakt — niet als order op de exchange gezet).
Valt het bot-proces uit, dan liggen je posities zonder actieve bewaking.
Ga dus alleen live met bedragen die je kunt missen, of vraag eerst om de
live-modus verder uit te laten bouwen.
            """
        )

    with st.expander("👀 Zie ik de trades terug op Bitvavo/Kraken?"):
        st.markdown(
            """
- **Paper-modus (nu): nee.** Alle orders zijn gesimuleerd en bestaan
  alleen in de database van de bot. Op de exchange is niets te zien —
  de bot *leest* er alleen prijzen.
- **Live-modus: ja.** Orders verschijnen dan gewoon in je
  Bitvavo/Kraken-account onder open orders en handelsgeschiedenis, en
  je saldo verandert echt mee.
            """
        )

    with st.expander("📣 Meldingen op je telefoon (Telegram / Discord)"):
        st.markdown(
            """
De bot kan pushen bij elke fill, stop-loss/take-profit, de
drawdown-noodrem, verbroken verbindingen en een dagelijkse PnL-samenvatting.

**Telegram:**
1. Open Telegram → zoek **@BotFather** → stuur `/newbot` → je krijgt een
   token (`1234567:AAH...`) → 🔐 `TELEGRAM_BOT_TOKEN`
2. Zoek **@userinfobot** → stuur een bericht → je id (getal) →
   🔐 `TELEGRAM_CHAT_ID`
3. Stuur je nieuwe bot zelf één berichtje (anders mag hij jou niets sturen)
4. Test hierboven bij **Control & Settings → Send test notification**

**Discord:** rechtsklik op een kanaal → Kanaal bewerken → Integraties →
Webhooks → nieuwe webhook → URL kopiëren → 🔐 `DISCORD_WEBHOOK_URL`
            """
        )

    with st.expander("🔁 Op twee exchanges tegelijk? (arbitrage — géén wash trading)"):
        st.markdown(
            """
**Even de termen scherp:**
- **Arbitrage** = prijsverschillen tussen twee exchanges benutten (op de
  goedkope kopen, op de dure verkopen). Dat is **legaal** en waarschijnlijk
  wat je bedoelt.
- **Wash trading** = met jezelf handelen om nepvolume of nepprijzen te
  creëren. Dat is **marktmanipulatie en verboden** — dat doet en
  ondersteunt deze bot niet.

**Kan deze bot arbitrage?** Nog niet: één bot-proces handelt op één
exchange. Je *kunt* wel twee losse bots draaien (één op Bitvavo, één op
Kraken, elk met eigen database), maar dat is onafhankelijk handelen, geen
arbitrage.

**Realistisch beeld:** arbitrage tussen Bitvavo en Kraken klinkt
aantrekkelijk, maar de prijsverschillen zijn meestal kleiner dan
2× de handelsfees, en professionele partijen met co-locatie zijn je
vrijwel altijd voor. Wil je het toch verkennen, vraag dan om een
arbitrage-monitor die eerst alleen *meet* hoe vaak een winstgevend
verschil (na fees) echt voorkomt — dat is de zinnige eerste stap.
            """
        )

    with st.expander("🛠️ Problemen oplossen"):
        st.markdown(
            """
- **Status OFFLINE** — het bot-proces draait niet. Op Render: check
  Events/Logs; lokaal: start `python main.py`.
- **Deploy mislukt op Render** — meestal een ontbrekende environment
  variable (bijv. `DASHBOARD_PASSWORD`): Environment → Edit → variabele
  toevoegen → Save, daarna Manual Deploy → *Deploy latest commit*.
- **Instelling wordt geweigerd** — de bot valideert alles; bijv. grid
  spacing onder de fee-vloer (2× maker fee + 0,1%) wordt bewust
  geblokkeerd omdat elke cyclus dan verlies draait.
- **Bot herstart / server opnieuw opgestart?** Geen probleem: alle
  posities, orders en het grid staan in de database en worden hersteld.
- **Logs bekijken:** tabblad *Live Logs* hierboven, of op Render onder
  *Logs*.
            """
        )


def render_logs_tab() -> None:
    """Tail of the bot's rotating log file."""
    st.subheader("Live Logs")
    log_path = Path(get_settings_cached().log_file)
    if not log_path.exists():
        st.info(f"Log file `{log_path}` not found yet — start the bot first.")
        return
    lines = st.slider("Lines to show", 20, 500, 100, step=20)
    level = st.selectbox("Filter level", ["ALL", "INFO", "WARNING", "ERROR"], index=0)
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if level != "ALL":
        content = [line for line in content if f"| {level:<8}|" in line or f"| {level}" in line]
    st.code("\n".join(content[-lines:]) or "(no matching lines)", language="log")


def require_password() -> bool:
    """Gate the dashboard behind ``DASHBOARD_PASSWORD`` when configured.

    With no password configured (e.g. local development) the dashboard stays
    open. Otherwise a login form is shown until the correct password is
    entered; the result is kept in the Streamlit session, so each browser
    session logs in once. Comparison is constant-time via ``hmac``.
    """
    expected = get_settings_cached().dashboard_password
    if not expected:
        return True
    if st.session_state.get("auth_ok", False):
        return True

    st.markdown("## 🔒 Trading Bot Dashboard")
    st.caption("This dashboard is password-protected.")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in", type="primary")
    if submitted:
        if hmac.compare_digest(entered, expected):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


def main() -> None:
    """Compose the dashboard page."""
    st.set_page_config(
        page_title="Trading Bot Dashboard", page_icon="🤖", layout="wide"
    )
    st.markdown(_CSS, unsafe_allow_html=True)

    if not require_password():
        return

    if not Path(get_settings_cached().db_path).exists():
        st.warning(
            "Database not found yet. Start the bot once (`python main.py`) to "
            "create it — the dashboard will then show live state."
        )

    cfg = load_config()
    render_header(cfg)

    tabs = st.tabs(
        ["⚙️ Control & Settings", "💼 Portfolio & Orders",
         "📊 Analytics & Grid", "📜 Live Logs", "🧪 Backtesting",
         "🔬 Optimizer", "❓ Help"]
    )
    with tabs[0]:
        render_settings_tab(cfg)
    with tabs[1]:
        render_portfolio_tab(cfg)
    with tabs[2]:
        render_analytics_tab(cfg)
    with tabs[3]:
        render_logs_tab()
    with tabs[4]:
        render_backtest_tab(cfg)
    with tabs[5]:
        render_optimizer_tab(cfg)
    with tabs[6]:
        render_help_tab()

    with st.sidebar:
        st.markdown("### Refresh")
        auto = st.toggle("Auto-refresh", value=False)
        interval = st.slider("Interval (s)", 2, 30, 5)
        if st.button("🔄 Refresh now", width="stretch"):
            st.rerun()
        st.caption("The dashboard reads the bot's SQLite database; the bot "
                   "applies saved settings within its config poll interval.")
        if get_settings_cached().dashboard_password:
            if st.button("🚪 Log out", width="stretch"):
                st.session_state["auth_ok"] = False
                st.rerun()

    if auto:
        time.sleep(interval)
        st.rerun()


main()
