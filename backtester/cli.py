"""Command-line backtest runner.

Examples::

    python -m backtester.cli --symbol BTC/EUR --timeframe 5m --days 30 --strategy grid
    python -m backtester.cli --symbol ETH/EUR --timeframe 1h --days 90 \
        --strategy sma_crossover --sma-fast 12 --sma-slow 48 --capital 5000
"""

from __future__ import annotations

import argparse
import asyncio
import time

from config.config import Settings
from backtester.data_loader import OHLCVDataLoader, candles_from_df
from backtester.engine import BacktestEngine
from backtester.metrics import BacktestMetrics, compute_metrics
from backtester.models import TIMEFRAMES_MS, BacktestResult
from strategies.base import BaseStrategy
from strategies.grid_trading import GridTradingStrategy
from strategies.sma_crossover import SmaCrossoverStrategy
from utils.logging import setup_logging


def build_parser() -> argparse.ArgumentParser:
    """The CLI argument parser (exposed for testing/documentation)."""
    parser = argparse.ArgumentParser(
        prog="python -m backtester.cli",
        description="Backtest a strategy on historical OHLCV data.",
    )
    parser.add_argument("--symbol", default="BTC/EUR", help="Trading pair (default: BTC/EUR)")
    parser.add_argument(
        "--timeframe", default="1h", choices=sorted(TIMEFRAMES_MS),
        help="Candle timeframe (default: 1h)",
    )
    parser.add_argument("--days", type=float, default=30.0, help="Lookback window in days")
    parser.add_argument(
        "--strategy", default="grid", choices=["grid", "sma_crossover"],
        help="Strategy to test (default: grid)",
    )
    parser.add_argument("--exchange", default="bitvavo", choices=["bitvavo", "kraken"])
    parser.add_argument("--capital", type=float, default=10_000.0, help="Initial capital (€)")
    parser.add_argument("--maker-fee", type=float, default=0.0015, help="Maker fee fraction")
    parser.add_argument("--taker-fee", type=float, default=0.0025, help="Taker fee fraction")
    parser.add_argument("--slippage", type=float, default=0.0005, help="Market-order slippage fraction")
    parser.add_argument("--grid-levels", type=int, default=5)
    parser.add_argument("--grid-spacing", type=float, default=0.005, help="Grid spacing fraction")
    parser.add_argument("--grid-order-size", type=float, default=100.0, help="Grid order size (€)")
    parser.add_argument("--sma-fast", type=int, default=10)
    parser.add_argument("--sma-slow", type=int, default=30)
    parser.add_argument("--stop-loss", type=float, default=0.02, help="Stop-loss fraction")
    parser.add_argument("--take-profit", type=float, default=0.04, help="Take-profit fraction")
    parser.add_argument("--cache-dir", default="data/historical")
    parser.add_argument("--output", default=None, help="Optional CSV path for the trade history")
    return parser


def _build_strategy(args: argparse.Namespace) -> BaseStrategy:
    if args.strategy == "grid":
        return GridTradingStrategy(
            symbol=args.symbol,
            levels=args.grid_levels,
            spacing_pct=args.grid_spacing,
            order_quote_size=args.grid_order_size,
        )
    return SmaCrossoverStrategy(
        symbol=args.symbol,
        fast_period=args.sma_fast,
        slow_period=args.sma_slow,
        order_quote_size=args.grid_order_size,
    )


def _print_report(result: BacktestResult, metrics: BacktestMetrics) -> None:
    duration_days = result.duration_seconds / 86_400
    dd_hours = metrics.max_drawdown_duration_s / 3600
    print()
    print(f"═══ Backtest report — {result.symbol} {result.timeframe} ═══")
    print(f"  Candles / period      {len(result.timestamps)} / {duration_days:.1f} days")
    print(f"  Initial capital       €{result.initial_capital:,.2f}")
    print(f"  Final equity          €{metrics.final_equity:,.2f}")
    print(f"  Total PnL             €{metrics.total_pnl:+,.2f} ({metrics.total_return_pct:+.2f}%)")
    print(f"  Buy & hold return     {metrics.benchmark_return_pct:+.2f}%")
    print(f"  CAGR                  {metrics.cagr_pct:+.2f}%")
    print(f"  Max drawdown          {metrics.max_drawdown_pct:.2f}% ({dd_hours:.1f}h)")
    print(f"  Sharpe / Sortino      {metrics.sharpe_ratio:.2f} / {metrics.sortino_ratio:.2f}")
    print(f"  Win rate              {metrics.win_rate_pct:.1f}% "
          f"({metrics.winning_trades}W / {metrics.losing_trades}L)")
    print(f"  Profit factor         {metrics.profit_factor:.2f}")
    print(f"  Trades / fees         {metrics.total_trades} / €{metrics.total_fees:,.2f}")
    print()


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, run the backtest, and print the report."""
    args = build_parser().parse_args(argv)
    setup_logging("INFO")

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.days * 86_400_000)

    loader = OHLCVDataLoader(exchange_id=args.exchange, cache_dir=args.cache_dir)
    df = loader.load(args.symbol, args.timeframe, start_ms, end_ms)
    if df.empty:
        raise SystemExit("No historical data returned for the requested range.")
    candles = candles_from_df(df)

    settings = Settings(
        _env_file=None,
        exchange=args.exchange,
        symbols=[args.symbol],
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
    )
    engine = BacktestEngine(
        strategy=_build_strategy(args),
        settings=settings,
        initial_capital=args.capital,
        maker_fee_rate=args.maker_fee,
        taker_fee_rate=args.taker_fee,
        slippage_rate=args.slippage,
    )
    result = asyncio.run(engine.run(candles, timeframe=args.timeframe))
    _print_report(result, compute_metrics(result))

    if args.output:
        from dataclasses import asdict

        import pandas as pd

        pd.DataFrame([asdict(t) | {"cost": t.cost} for t in result.trades]).to_csv(
            args.output, index=False
        )
        print(f"Trade history written to {args.output}")


if __name__ == "__main__":
    main()
