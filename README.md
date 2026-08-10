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
  (`on_ticker`, `on_bar_close`, `on_orderbook`, `generate_signals`), with a
  robust **grid trading** strategy (auto re-anchoring + inventory cap) and a
  simple **SMA crossover** included.
- 🕯️ **Live candle aggregation** — raw WebSocket ticks are bucketed into
  fixed-timeframe OHLCV bars (`BAR_TIMEFRAME`); indicators compute on
  completed bar closes only, so live behavior matches backtests exactly
  (no tick-rate distortion).
- 💶 **Fee-aware accounting** — realized PnL and win rates are **net** of
  maker/taker fees and slippage, and grid configurations whose spacing can't
  cover round-trip fees are rejected outright.
- 📣 **Notifications** — Telegram / Discord webhook alerts for fills, SL/TP
  triggers, drawdown circuit-breaker events, WebSocket disconnects
  (throttled), and a periodic PnL digest.
- 💾 **SQLite persistence** (`aiosqlite`) — balances, positions, orders, grid
  levels, and the equity curve survive restarts; the grid resumes exactly
  where it left off.
- 🖥️ **Streamlit dashboard** — live metrics, start/pause/stop control,
  runtime setting changes (no restart), portfolio & order tables, equity and
  grid-level Plotly charts, and a live log viewer.
- 🧪 **Historical backtesting** — CCXT OHLCV download with local SQLite
  caching, intra-candle order matching (limits fill when the candle range
  crosses them, market/SL exits pay slippage), buy-&-hold benchmark, full
  metrics (CAGR, max drawdown, Sharpe/Sortino, win rate, profit factor), a
  CLI, and a dedicated dashboard tab with interactive Plotly analysis.
- 🧹 **Graceful shutdown** — SIGINT/SIGTERM flush all state to SQLite, close
  WebSockets and the database, and exit with code 0.
- 📝 **Structured logging** via `loguru`: live ticks, simulated fills, a
  periodic PnL summary, and a rotating log file for the dashboard.

## Project layout

```
backtester/   Historical data loader + cache, backtest engine, metrics, CLI
config/       Settings via pydantic-settings + .env
connectors/   Exchange abstraction + ccxt.pro connector (Bitvavo, Kraken)
dashboard/    Streamlit web dashboard (reads/writes the SQLite database)
engine/       Domain models, paper trading engine, execution router
storage/      Async SQLite layer, engine persistence, runtime config sync
strategies/   BaseStrategy, grid trading, SMA crossover
risk/         RiskManager (allocation, drawdown, SL/TP enforcement)
utils/        loguru logging setup, graceful shutdown, backoff helper
tests/        pytest unit tests (fills, risk, signals, persistence, backtests)
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

### Guardrails & strategy behavior

- **Fee floor**: grid spacing must exceed `2 × MAKER_FEE_RATE + 0.1%` — a
  completed grid cycle pays two maker fees, so tighter spacing loses money by
  construction. Enforced at startup, on runtime config changes, and in the
  dashboard form; the backtester warns.
- **Grid re-anchoring** (`GRID_AUTO_REANCHOR`): when price drifts beyond
  `GRID_REANCHOR_FACTOR × grid width` from the anchor, resting unfilled buys
  are canceled and a fresh grid is built around the new price. Levels holding
  inventory keep their sell orders and retire once sold.
- **Inventory cap** (`GRID_MAX_INVENTORY_QUOTE`): hard ceiling on committed
  capital (held inventory at entry value + resting buys), defaulting to one
  full grid — prevents unbounded accumulation on a falling market across
  re-anchors.
- **Net PnL**: realized PnL, win rates, and profit factors are net of all
  fees (each sale is charged its own fee plus a proportional share of the
  entry fees) and slippage — a trade that only wins gross of costs counts
  as a loss.

### Notifications

Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (via
[@BotFather](https://t.me/BotFather)) and/or `DISCORD_WEBHOOK_URL` in `.env`.
The bot then pushes: order fills, stop-loss/take-profit triggers, drawdown
circuit-breaker trips and recoveries, WebSocket disconnect/reconnect events
(throttled to avoid storms), a startup banner, and a PnL digest every
`DIGEST_INTERVAL_HOURS`. The dashboard's **Control & Settings** tab has a
*Send test notification* button to verify the setup.

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

## Backtesting

Test a strategy on historical data before letting it trade — from the CLI:

```bash
python -m backtester.cli --symbol BTC/EUR --timeframe 5m --days 30 --strategy grid
python -m backtester.cli --symbol ETH/EUR --timeframe 1h --days 90 \
    --strategy sma_crossover --sma-fast 12 --sma-slow 48 --capital 5000 \
    --output trades.csv
```

or interactively from the dashboard's **🧪 Backtesting** tab (symbol,
timeframe, date range, capital, strategy parameters, fee and slippage
sliders), which renders metric cards (bot vs buy-&-hold, max drawdown,
Sharpe, win rate), an equity-curve comparison, a price chart with entry/exit
markers, a drawdown chart, and a downloadable trade history.

How the simulation works:

- OHLCV candles are downloaded via CCXT with pagination and cached in
  `data/historical/{exchange}_{symbol}_{timeframe}.sqlite`; repeated runs
  only fetch the missing head/tail of the requested range.
- The backtest reuses the **same** `PaperEngine`, `RiskManager`, and strategy
  classes as live trading. Each candle is replayed as an intra-candle price
  path (`open → low → high → close` for up candles, mirrored for down
  candles): resting limit orders fill at their limit price (maker fee) when
  the candle range crosses them; market orders and stop-loss / take-profit
  exits — evaluated at every path point, so intra-candle spikes trigger
  them — fill with configurable slippage (default 0.05%) plus the taker fee.
- Strategies only see candle closes, mirroring live indicator behavior, and
  a buy-&-hold benchmark (all-in at the first open, taker fee applied) is
  tracked alongside for comparison.

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
