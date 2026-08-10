"""Aggregates raw WebSocket ticks into fixed-timeframe OHLCV bars.

Live strategies must compute indicators on *completed bars*, not raw ticks —
ticks arrive at irregular, volume-dependent rates, so a "10-period SMA" over
ticks means something different every minute. The
:class:`CandleAggregator` buckets ticks into time-aligned bars matching the
backtester's candle timeframe, which removes that distortion entirely: an
indicator sees the same series live as it does in a backtest.

Concurrency note: the aggregator is designed for use from a single asyncio
event loop (the bot's ticker callback); its operations are plain synchronous
mutations with no awaits, so they are atomic with respect to other coroutines
and need no locking.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Supported timeframes and their duration in milliseconds.
TIMEFRAMES_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def timeframe_to_ms(timeframe: str) -> int:
    """Duration of one bar in milliseconds for a timeframe string."""
    try:
        return TIMEFRAMES_MS[timeframe]
    except KeyError:
        raise ValueError(
            f"unsupported timeframe {timeframe!r}; expected one of "
            f"{sorted(TIMEFRAMES_MS)}"
        ) from None


def timeframe_to_seconds(timeframe: str) -> float:
    """Duration of one bar in seconds for a timeframe string."""
    return timeframe_to_ms(timeframe) / 1000.0


@dataclass(slots=True)
class Bar:
    """One OHLCV bar. ``timestamp`` is the bucket-aligned open time (seconds)."""

    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class CandleAggregator:
    """Buckets a stream of ticks into completed OHLCV bars.

    Args:
        timeframe: Bar duration, e.g. ``"1m"``, ``"5m"``, ``"15m"``.

    Usage::

        aggregator = CandleAggregator("1m")
        bar = aggregator.add_tick(price, timestamp)
        if bar is not None:      # a bar just completed
            await strategy.on_bar_close(bar)
    """

    def __init__(self, timeframe: str = "1m") -> None:
        self.timeframe = timeframe
        self._tf_seconds = timeframe_to_seconds(timeframe)
        self._bar: Bar | None = None

    @property
    def current_bar(self) -> Bar | None:
        """The in-progress (not yet completed) bar, if any."""
        return self._bar

    def add_tick(self, price: float, timestamp: float, volume: float = 0.0) -> Bar | None:
        """Ingest one tick; return the completed bar when a bucket rolls over.

        The first tick of a new time bucket closes the previous bucket's bar
        (gaps simply skip empty buckets — no synthetic bars are emitted) and
        opens a new one. Ticks inside the current bucket update its
        high/low/close and accumulate volume.
        """
        bucket = timestamp - (timestamp % self._tf_seconds)
        if self._bar is None:
            self._bar = Bar(bucket, price, price, price, price, volume)
            return None
        if bucket > self._bar.timestamp:
            completed = self._bar
            self._bar = Bar(bucket, price, price, price, price, volume)
            return completed
        bar = self._bar
        bar.high = max(bar.high, price)
        bar.low = min(bar.low, price)
        bar.close = price
        bar.volume += volume
        return None

    def force_close(self) -> Bar | None:
        """Close and return the in-progress bar (e.g. on shutdown), if any."""
        bar, self._bar = self._bar, None
        return bar
