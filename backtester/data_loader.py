"""Historical OHLCV downloading with pagination and local SQLite caching.

Candles are fetched via CCXT's REST ``fetch_ohlcv`` (paginated, rate-limit
aware) and cached in per-dataset SQLite files under ``data/historical/``,
keyed ``{exchange}_{symbol}_{timeframe}.sqlite`` — repeated backtests over
the same range never hit the exchange again. Only the missing head/tail of a
requested range is downloaded; already-cached rows are reused.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Protocol

import pandas as pd
from loguru import logger

from backtester.models import Candle, timeframe_to_ms

_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
_PAGE_LIMIT = 1000


class SupportsFetchOHLCV(Protocol):
    """Minimal CCXT-exchange surface the loader needs (easy to fake in tests)."""

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = ...,
        since: int | None = ...,
        limit: int | None = ...,
    ) -> list[list[float]]: ...


class OHLCVDataLoader:
    """Downloads and caches historical OHLCV data for backtesting.

    Args:
        exchange_id: CCXT exchange id (``"bitvavo"`` / ``"kraken"``); only
            used when no ``exchange`` instance is injected.
        cache_dir: Directory for the per-dataset SQLite cache files.
        exchange: Optional pre-built exchange-like object (dependency
            injection for tests); must expose ``fetch_ohlcv``.
    """

    def __init__(
        self,
        exchange_id: str = "bitvavo",
        cache_dir: str | Path = "data/historical",
        exchange: SupportsFetchOHLCV | None = None,
    ) -> None:
        self._exchange_id = exchange_id
        self._cache_dir = Path(cache_dir)
        self._exchange = exchange

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> pd.DataFrame:
        """Return OHLCV candles covering ``[start_ms, end_ms]`` (inclusive).

        Serves from the local cache where possible and downloads only the
        missing leading/trailing spans from the exchange.

        Returns:
            DataFrame with columns ``timestamp`` (ms), ``open``, ``high``,
            ``low``, ``close``, ``volume``, sorted ascending.
        """
        if end_ms <= start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        tf_ms = timeframe_to_ms(timeframe)
        end_ms = min(end_ms, int(time.time() * 1000))

        with self._cache_conn(symbol, timeframe) as conn:
            cached_min, cached_max = self._cached_bounds(conn)
            spans: list[tuple[int, int]] = []
            if cached_min is None or cached_max is None:
                spans.append((start_ms, end_ms))
            else:
                if start_ms < cached_min - tf_ms:
                    spans.append((start_ms, cached_min - tf_ms))
                if end_ms > cached_max + tf_ms:
                    spans.append((cached_max + tf_ms, end_ms))

            for span_start, span_end in spans:
                rows = self._fetch_range(symbol, timeframe, span_start, span_end)
                if rows:
                    self._store(conn, rows)
                    logger.info(
                        "Cached {} candles for {} {} [{} … {}]",
                        len(rows), symbol, timeframe,
                        pd.Timestamp(rows[0][0], unit="ms"),
                        pd.Timestamp(rows[-1][0], unit="ms"),
                    )

            return self._read(conn, start_ms, end_ms)

    def load_candles(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> list[Candle]:
        """Like :meth:`load` but returning :class:`Candle` objects
        (timestamps converted to seconds) for the backtest engine."""
        return candles_from_df(self.load(symbol, timeframe, start_ms, end_ms))

    # ------------------------------------------------------------------
    # Exchange access
    # ------------------------------------------------------------------
    def _get_exchange(self) -> SupportsFetchOHLCV:
        if self._exchange is None:
            import ccxt  # deferred: keep import cost out of cache-only paths

            exchange_cls = getattr(ccxt, self._exchange_id)
            exchange = exchange_cls({"enableRateLimit": True})
            # ccxt disables requests' trust_env; re-enable it so standard
            # HTTP(S)_PROXY / REQUESTS_CA_BUNDLE env vars work (mirrors the
            # aiohttp_trust_env setting in the WebSocket connector).
            session = getattr(exchange, "session", None)
            if session is not None:
                session.trust_env = True
            self._exchange = exchange
        return self._exchange

    def _fetch_range(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> list[list[float]]:
        """Fetch ``[start_ms, end_ms]`` from the exchange, paginating forward."""
        exchange = self._get_exchange()
        tf_ms = timeframe_to_ms(timeframe)
        rows: dict[int, list[float]] = {}
        since = start_ms
        while since <= end_ms:
            batch = exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=since, limit=_PAGE_LIMIT
            )
            if not batch:
                break
            for row in batch:
                ts = int(row[0])
                if start_ms <= ts <= end_ms:
                    rows[ts] = row
            last_ts = int(batch[-1][0])
            next_since = last_ts + tf_ms
            if next_since <= since:  # defensive: never loop on a stuck cursor
                break
            since = next_since
            if len(batch) < _PAGE_LIMIT:
                break
        return [rows[ts] for ts in sorted(rows)]

    # ------------------------------------------------------------------
    # SQLite cache
    # ------------------------------------------------------------------
    def cache_path(self, symbol: str, timeframe: str) -> Path:
        """Cache file for one dataset, keyed ``exchange_symbol_timeframe``."""
        safe_symbol = symbol.replace("/", "-")
        return self._cache_dir / f"{self._exchange_id}_{safe_symbol}_{timeframe}.sqlite"

    def _cache_conn(self, symbol: str, timeframe: str) -> sqlite3.Connection:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.cache_path(symbol, timeframe))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ohlcv ("
            "timestamp INTEGER PRIMARY KEY, open REAL NOT NULL, "
            "high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, "
            "volume REAL NOT NULL)"
        )
        return conn

    @staticmethod
    def _cached_bounds(conn: sqlite3.Connection) -> tuple[int | None, int | None]:
        row = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM ohlcv").fetchone()
        return (
            None if row[0] is None else int(row[0]),
            None if row[1] is None else int(row[1]),
        )

    @staticmethod
    def _store(conn: sqlite3.Connection, rows: list[list[float]]) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO ohlcv (timestamp, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                 float(r[5]) if len(r) > 5 and r[5] is not None else 0.0)
                for r in rows
            ],
        )
        conn.commit()

    @staticmethod
    def _read(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> pd.DataFrame:
        df = pd.read_sql_query(
            "SELECT timestamp, open, high, low, close, volume FROM ohlcv "
            "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
            conn,
            params=(start_ms, end_ms),
        )
        return df[_COLUMNS]


def candles_from_df(df: pd.DataFrame) -> list[Candle]:
    """Convert a loader DataFrame (ms timestamps) into :class:`Candle` objects."""
    return [
        Candle(
            timestamp=float(row.timestamp) / 1000.0,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for row in df.itertuples(index=False)
    ]
