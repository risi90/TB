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
    .block-container { padding-top: 1.2rem; }
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

    left, b1, b2, b3 = st.columns([3, 1, 1, 1])
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


def main() -> None:
    """Compose the dashboard page."""
    st.set_page_config(
        page_title="Trading Bot Dashboard", page_icon="🤖", layout="wide"
    )
    st.markdown(_CSS, unsafe_allow_html=True)

    if not Path(get_settings_cached().db_path).exists():
        st.warning(
            "Database not found yet. Start the bot once (`python main.py`) to "
            "create it — the dashboard will then show live state."
        )

    cfg = load_config()
    render_header(cfg)

    tab_settings, tab_portfolio, tab_analytics, tab_logs = st.tabs(
        ["⚙️ Control & Settings", "💼 Portfolio & Orders",
         "📊 Analytics & Grid", "📜 Live Logs"]
    )
    with tab_settings:
        render_settings_tab(cfg)
    with tab_portfolio:
        render_portfolio_tab(cfg)
    with tab_analytics:
        render_analytics_tab(cfg)
    with tab_logs:
        render_logs_tab()

    with st.sidebar:
        st.markdown("### Refresh")
        auto = st.toggle("Auto-refresh", value=False)
        interval = st.slider("Interval (s)", 2, 30, 5)
        if st.button("🔄 Refresh now", width="stretch"):
            st.rerun()
        st.caption("The dashboard reads the bot's SQLite database; the bot "
                   "applies saved settings within its config poll interval.")

    if auto:
        time.sleep(interval)
        st.rerun()


main()
