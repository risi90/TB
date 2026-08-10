# TB — Async Crypto Trading Bot Framework

A modular, fully-typed **Python 3.11+** crypto trading bot framework built on
[CCXT Pro](https://docs.ccxt.com/#/ccxt.pro.manual) with a **safety-first
paper trading engine**, real-time WebSocket market data, and mandatory risk
management.

> ⚠️ **Disclaimer**: This is a framework for experimentation and education.
> Paper trading is the default; live trading is at your own risk.

## Features

- 🛡️ **Paper mode by default** — real orders are hard-blocked unless
  `PAPER_TRADING=False` *and* API keys are configured.
- 📡 **Real-time WebSocket data** via `ccxt.pro` for **Bitvavo** and **Kraken**,
  with automatic reconnection (exponential backoff + jitter) and HTTP 429
  rate-limit handling.
- 🧪 **Paper trading engine** — matches simulated limit/market orders against
  live bid/ask, reserves funds to prevent double-spending, applies maker/taker
  fees (e.g. 0.15% / 0.25%), and tracks equity, positions, and
  realized/unrealized PnL in real time.
- 🚦 **Risk manager** — every strategy order passes `RiskManager.validate_order()`:
  max allocation per trade, max portfolio drawdown circuit breaker, and
  mandatory stop-loss / take-profit stamped on every entry.
- 📈 **Strategies** — pluggable via an abstract `BaseStrategy`
  (`on_ticker`, `on_orderbook`, `generate_signals`), with a robust
  **grid trading** strategy and a simple **SMA crossover** included.
- 💾 **SQLite persistence** (`aiosqlite`) — balances, positions, orders, grid
  levels, and the equity curve survive restarts; the grid resumes exactly
  where it left off.
- 🖥️ **Streamlit dashboard** — live metrics, start/pause/stop control,
  runtime setting changes (no restart), portfolio & order tables, equity and
  grid-level Plotly charts, and a live log viewer.
- 🧹 **Graceful shutdown** — SIGINT/SIGTERM flush all state to SQLite, close
  WebSockets and the database, and exit with code 0.
- 📝 **Structured logging** via `loguru`: live ticks, simulated fills, a
  periodic PnL summary, and a rotating log file for the dashboard.

## Project layout

```
config/       Settings via pydantic-settings + .env
connectors/   Exchange abstraction + ccxt.pro connector (Bitvavo, Kraken)
dashboard/    Streamlit web dashboard (reads/writes the SQLite database)
engine/       Domain models, paper trading engine, execution router
storage/      Async SQLite layer, engine persistence, runtime config sync
strategies/   BaseStrategy, grid trading, SMA crossover
risk/         RiskManager (allocation, drawdown, SL/TP enforcement)
utils/        loguru logging setup, graceful shutdown, backoff helper
tests/        pytest unit tests (fills, risk, signals, persistence, config)
main.py       Async entry point
```

## Quick start

```bash
# 1. Install dependencies (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env        # defaults are safe: paper mode, BTC/EUR on Bitvavo

# 3. Run the bot (no API keys needed for paper trading — market data is public)
python main.py

# 4. In a second terminal: run the web dashboard
streamlit run dashboard/app.py
```

The bot and the dashboard are **separate processes** that communicate through
the SQLite database (`data/bot_state.db` by default):

- `python main.py` runs the trading loop, streams market data, and persists
  every state change (orders, fills, balances, grid levels, equity curve).
- `streamlit run dashboard/app.py` serves the web UI (default
  http://localhost:8501). It reads state directly from SQLite and writes
  runtime settings / start-pause-stop commands into the `bot_config` table,
  which the bot picks up within `CONFIG_POLL_INTERVAL` seconds — no restart
  required. Settings saved in the dashboard also survive bot restarts and
  take precedence over `.env` for the managed keys.

Stopping: press `Ctrl-C` in the bot terminal or use the dashboard's **Stop**
button — either way the bot stops taking entries, flushes all in-memory state
to SQLite, closes WebSockets and the database, and exits with code 0. On the
next start it rehydrates balances, positions, resting orders, and the active
grid, and continues seamlessly.

You'll see live ticks, simulated order fills, and periodic PnL summaries:

```
12:00:01.123 | INFO     | engine.paper_engine | FILL BUY limit 0.00234000 BTC/EUR @ 42750.00 (fee 0.1500 EUR)
12:00:30.456 | INFO     | main | PNL | equity 9998.75 EUR | realized +1.20 | unrealized -2.45 | fees 0.30 | drawdown 0.01% | open orders 4 | positions 1
```

Stop with `Ctrl-C` — the bot shuts down gracefully and prints final equity.

## Configuration

All settings live in `.env` (see `.env.example` for the full list):

| Variable | Default | Description |
|---|---|---|
| `PAPER_TRADING` | `True` | **Safety switch.** Must be explicitly `False` for live orders |
| `EXCHANGE` | `bitvavo` | `bitvavo` or `kraken` |
| `SYMBOLS` | `BTC/EUR` | Comma-separated trading pairs |
| `STRATEGY` | `grid` | `grid` or `sma_crossover` |
| `MAKER_FEE_RATE` / `TAKER_FEE_RATE` | `0.0015` / `0.0025` | Simulated trading fees |
| `MAX_ALLOCATION_PCT` | `0.10` | Max fraction of equity per order |
| `MAX_DRAWDOWN_PCT` | `0.15` | Drawdown from peak equity that blocks new entries |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | `0.02` / `0.04` | Mandatory protective levels on entries |
| `DB_PATH` | `data/bot_state.db` | SQLite database shared with the dashboard |
| `CONFIG_POLL_INTERVAL` | `5` | Seconds between runtime-config polls |
| `LOG_FILE` | `logs/bot.log` | Rotating log file (dashboard Live Logs tab) |

Symbols, grid parameters, and risk limits can also be changed at runtime from
the dashboard's **Control & Settings** tab; changing the exchange requires a
bot restart.

### Going live (not recommended until thoroughly paper-tested)

Live trading requires **all** of the following, enforced in
`engine/execution.py` (`ExecutionRouter`) — there is no code path around it:

1. `PAPER_TRADING=False` in `.env`
2. Non-empty `API_KEY` and `API_SECRET`

If either is missing, order submission raises `LiveTradingBlocked`.

## How it works

```
WebSocket ticker (ccxt.pro)
        │
        ▼
TradingApp.handle_ticker
        ├─ PaperEngine.process_ticker()      ← match resting limit orders
        ├─ RiskManager.update_equity()       ← drawdown tracking
        ├─ RiskManager.check_protective_exit ← enforce SL/TP on positions
        └─ Strategy.on_ticker() → generate_signals()
                    │
                    ▼
        RiskManager.validate_order()          ← allocation / drawdown / SL-TP
                    │ approved
                    ▼
        ExecutionRouter.submit()              ← paper engine OR live exchange
```

- **Market orders** fill immediately at the live ask (buy) / bid (sell) with
  the taker fee.
- **Limit orders** rest until the live book crosses their price (maker fee);
  limits that cross at placement fill immediately as taker.
- Buys reserve quote funds (including worst-case fee) at placement; sells
  reserve base — the simulated account can never double-spend.

### Persistence & the dashboard

All durable state lives in one SQLite database (WAL mode, so the dashboard
reads while the bot writes):

| Table | Contents |
|---|---|
| `bot_config` | Live-adjustable settings, bot status, heartbeat, last prices |
| `positions` | Per-symbol position snapshots incl. SL/TP levels |
| `orders` | Full order history; open rows are rehydrated on startup |
| `grid_state` | Serialized grid rungs, order bindings, and cycle counts |
| `account_balances` | Free/reserved funds per currency |
| `equity_history` | Timestamped equity / realized / unrealized PnL curve |

On startup the bot seeds `bot_config` with its settings (without overwriting
dashboard edits), applies any stored overrides, restores the account and open
orders into the paper engine, and rehydrates each grid strategy — repairing
levels whose orders no longer exist and discarding state whose grid
configuration changed.

## Testing

```bash
pip install -r requirements.txt
pytest -v
```

The test suite covers paper fills (market/limit, maker/taker fees, fund
reservation, PnL), risk manager limits (allocation, drawdown circuit breaker,
mandatory SL/TP), strategy signals (grid lifecycle, SMA crossovers), the
live-trading hardblock, persistence (schema, state saving, engine and grid
rehydration after simulated restarts), and runtime config sync through the
database.

## Extending

Add a strategy by subclassing `strategies.base.BaseStrategy`:

```python
class MyStrategy(BaseStrategy):
    async def on_ticker(self, ticker: Ticker) -> None: ...
    async def on_orderbook(self, orderbook: OrderBook) -> None: ...
    def generate_signals(self) -> list[OrderRequest]: ...
```

then register it in `main.build_strategy()`. Every emitted `OrderRequest`
automatically passes through the risk manager before execution.
