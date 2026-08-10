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
- 📝 **Structured logging** via `loguru`: live ticks, simulated fills, and a
  periodic PnL summary.

## Project layout

```
config/       Settings via pydantic-settings + .env
connectors/   Exchange abstraction + ccxt.pro connector (Bitvavo, Kraken)
engine/       Domain models, paper trading engine, execution router
strategies/   BaseStrategy, grid trading, SMA crossover
risk/         RiskManager (allocation, drawdown, SL/TP enforcement)
utils/        loguru logging setup, exponential backoff helper
tests/        pytest unit tests (paper fills, risk, strategy signals)
main.py       Async entry point
```

## Quick start

```bash
# 1. Install dependencies (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env        # defaults are safe: paper mode, BTC/EUR on Bitvavo

# 3. Run (no API keys needed for paper trading — market data is public)
python main.py
```

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

## Testing

```bash
pip install -r requirements.txt
pytest -v
```

The test suite covers paper fills (market/limit, maker/taker fees, fund
reservation, PnL), risk manager limits (allocation, drawdown circuit breaker,
mandatory SL/TP), strategy signals (grid lifecycle, SMA crossovers), and the
live-trading hardblock.

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
