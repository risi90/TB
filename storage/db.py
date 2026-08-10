"""Async SQLite persistence layer built on ``aiosqlite``.

The database is the bot's durable state and the contract shared with the
Streamlit dashboard (which reads it with plain ``sqlite3``/``pandas``):

* ``bot_config`` — live-adjustable settings and status flags (key/value).
* ``positions`` — per-symbol position snapshots (plus SL/TP so protective
  levels survive restarts).
* ``orders`` — full order history; open rows are rehydrated on startup.
* ``grid_state`` — serialized grid strategy state per ``strategy_id``.
* ``account_balances`` — free/reserved funds per currency.
* ``equity_history`` — timestamped equity / PnL curve for analytics.

WAL journal mode is enabled so the dashboard can read concurrently while the
bot writes. Multi-statement writes go through :meth:`Database.transaction`
so they commit (or roll back) atomically. Timestamps are stored as Unix
epoch seconds (``REAL``).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from engine.models import Order, OrderSide, OrderStatus, OrderType, Position
from engine.paper_engine import Balance

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol       TEXT PRIMARY KEY,
    amount       REAL NOT NULL,
    entry_price  REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    entry_fees   REAL NOT NULL DEFAULT 0,
    stop_loss    REAL,
    take_profit  REAL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    type          TEXT NOT NULL,
    price         REAL,
    amount        REAL NOT NULL,
    status        TEXT NOT NULL,
    filled        REAL NOT NULL DEFAULT 0,
    fee           REAL NOT NULL DEFAULT 0,
    average_price REAL NOT NULL DEFAULT 0,
    stop_loss     REAL,
    take_profit   REAL,
    reduce_only   INTEGER NOT NULL DEFAULT 0,
    strategy_id   TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_updated ON orders(updated_at);

CREATE TABLE IF NOT EXISTS grid_state (
    strategy_id        TEXT PRIMARY KEY,
    symbol             TEXT NOT NULL,
    grid_config_json   TEXT NOT NULL,
    active_levels_json TEXT NOT NULL,
    updated_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS account_balances (
    currency   TEXT PRIMARY KEY,
    free       REAL NOT NULL,
    reserved   REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL NOT NULL,
    total_equity   REAL NOT NULL,
    realized_pnl   REAL NOT NULL,
    unrealized_pnl REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_history(timestamp);
"""


@dataclass(slots=True)
class GridStateRow:
    """One persisted grid strategy state row."""

    strategy_id: str
    symbol: str
    grid_config_json: str
    active_levels_json: str
    updated_at: float


class DatabaseError(Exception):
    """Raised for storage-layer misuse (e.g. queries before ``connect``)."""


class Database:
    """Async SQLite wrapper owning one connection and the schema.

    Args:
        path: Database file location; parent directories are created on
            :meth:`connect`.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        """The database file path."""
        return self._path

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Open the connection, enable WAL mode, and create the schema."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA)
        await self._migrate()

    async def _migrate(self) -> None:
        """Bring pre-existing databases up to the current schema."""
        async with self.conn.execute("PRAGMA table_info(positions)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "entry_fees" not in columns:
            await self.conn.execute(
                "ALTER TABLE positions ADD COLUMN entry_fees REAL NOT NULL DEFAULT 0"
            )

    async def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """The live connection; raises if :meth:`connect` was not called."""
        if self._conn is None:
            raise DatabaseError("Database.connect() must be awaited first")
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Group multiple statements into one atomic write transaction."""
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            await self.conn.execute("ROLLBACK")
            raise
        else:
            await self.conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # bot_config
    # ------------------------------------------------------------------
    async def set_config(self, key: str, value: str) -> None:
        """Upsert one ``bot_config`` entry."""
        await self.conn.execute(
            "INSERT INTO bot_config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, time.time()),
        )

    async def set_config_many(self, values: dict[str, str]) -> None:
        """Atomically upsert several ``bot_config`` entries."""
        now = time.time()
        async with self.transaction() as conn:
            await conn.executemany(
                "INSERT INTO bot_config (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                [(k, v, now) for k, v in values.items()],
            )

    async def seed_config(self, defaults: dict[str, str]) -> None:
        """Insert config entries only where the key does not exist yet."""
        now = time.time()
        async with self.transaction() as conn:
            await conn.executemany(
                "INSERT OR IGNORE INTO bot_config (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                [(k, v, now) for k, v in defaults.items()],
            )

    async def get_config(self, key: str) -> str | None:
        """Fetch one ``bot_config`` value, or ``None`` if absent."""
        async with self.conn.execute(
            "SELECT value FROM bot_config WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else str(row["value"])

    async def get_all_config(self) -> dict[str, str]:
        """Fetch the full ``bot_config`` table as a dict."""
        async with self.conn.execute("SELECT key, value FROM bot_config") as cursor:
            rows = await cursor.fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    # ------------------------------------------------------------------
    # positions
    # ------------------------------------------------------------------
    async def upsert_position(self, position: Position) -> None:
        """Upsert one position snapshot."""
        await self.conn.execute(
            "INSERT INTO positions (symbol, amount, entry_price, realized_pnl, "
            "entry_fees, stop_loss, take_profit, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET amount=excluded.amount, "
            "entry_price=excluded.entry_price, realized_pnl=excluded.realized_pnl, "
            "entry_fees=excluded.entry_fees, stop_loss=excluded.stop_loss, "
            "take_profit=excluded.take_profit, updated_at=excluded.updated_at",
            (
                position.symbol,
                position.amount,
                position.entry_price,
                position.realized_pnl,
                position.entry_fees,
                position.stop_loss,
                position.take_profit,
                time.time(),
            ),
        )

    async def load_positions(self) -> dict[str, Position]:
        """Load all persisted positions keyed by symbol."""
        async with self.conn.execute("SELECT * FROM positions") as cursor:
            rows = await cursor.fetchall()
        return {
            str(row["symbol"]): Position(
                symbol=str(row["symbol"]),
                amount=float(row["amount"]),
                entry_price=float(row["entry_price"]),
                realized_pnl=float(row["realized_pnl"]),
                entry_fees=float(row["entry_fees"]),
                stop_loss=None if row["stop_loss"] is None else float(row["stop_loss"]),
                take_profit=(
                    None if row["take_profit"] is None else float(row["take_profit"])
                ),
            )
            for row in rows
        }

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    async def upsert_order(self, order: Order, strategy_id: str | None = None) -> None:
        """Upsert one order row (insert on first sight, update thereafter)."""
        await self.conn.execute(
            "INSERT INTO orders (id, symbol, side, type, price, amount, status, "
            "filled, fee, average_price, stop_loss, take_profit, reduce_only, "
            "strategy_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "filled=excluded.filled, fee=excluded.fee, "
            "average_price=excluded.average_price, "
            "strategy_id=COALESCE(excluded.strategy_id, orders.strategy_id), "
            "updated_at=excluded.updated_at",
            (
                order.id,
                order.symbol,
                order.side.value,
                order.type.value,
                order.price,
                order.amount,
                order.status.value,
                order.filled,
                order.fee_paid,
                order.average_price,
                order.stop_loss,
                order.take_profit,
                int(order.reduce_only),
                strategy_id,
                order.created_at,
                order.updated_at,
            ),
        )

    async def load_open_orders(self) -> list[tuple[Order, str | None]]:
        """Load all orders with status ``open`` plus their strategy ids."""
        async with self.conn.execute(
            "SELECT * FROM orders WHERE status = ? ORDER BY created_at",
            (OrderStatus.OPEN.value,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            (self._row_to_order(row), row["strategy_id"] and str(row["strategy_id"]))
            for row in rows
        ]

    async def load_recent_orders(self, limit: int = 100) -> list[Order]:
        """Load the most recently updated orders (any status)."""
        async with self.conn.execute(
            "SELECT * FROM orders ORDER BY updated_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_order(row) for row in rows]

    @staticmethod
    def _row_to_order(row: aiosqlite.Row) -> Order:
        return Order(
            symbol=str(row["symbol"]),
            side=OrderSide(str(row["side"])),
            type=OrderType(str(row["type"])),
            amount=float(row["amount"]),
            price=None if row["price"] is None else float(row["price"]),
            stop_loss=None if row["stop_loss"] is None else float(row["stop_loss"]),
            take_profit=(
                None if row["take_profit"] is None else float(row["take_profit"])
            ),
            reduce_only=bool(row["reduce_only"]),
            id=str(row["id"]),
            status=OrderStatus(str(row["status"])),
            filled=float(row["filled"]),
            average_price=float(row["average_price"]),
            fee_paid=float(row["fee"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # grid_state
    # ------------------------------------------------------------------
    async def save_grid_state(
        self,
        strategy_id: str,
        symbol: str,
        grid_config_json: str,
        active_levels_json: str,
    ) -> None:
        """Upsert the serialized state of one grid strategy."""
        await self.conn.execute(
            "INSERT INTO grid_state (strategy_id, symbol, grid_config_json, "
            "active_levels_json, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(strategy_id) DO UPDATE SET symbol=excluded.symbol, "
            "grid_config_json=excluded.grid_config_json, "
            "active_levels_json=excluded.active_levels_json, "
            "updated_at=excluded.updated_at",
            (strategy_id, symbol, grid_config_json, active_levels_json, time.time()),
        )

    async def load_grid_state(self, strategy_id: str) -> GridStateRow | None:
        """Load one grid strategy state row, or ``None`` if absent."""
        async with self.conn.execute(
            "SELECT * FROM grid_state WHERE strategy_id = ?", (strategy_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return GridStateRow(
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            grid_config_json=str(row["grid_config_json"]),
            active_levels_json=str(row["active_levels_json"]),
            updated_at=float(row["updated_at"]),
        )

    async def delete_grid_state(self, strategy_id: str) -> None:
        """Remove one grid strategy state row (no-op if absent)."""
        await self.conn.execute(
            "DELETE FROM grid_state WHERE strategy_id = ?", (strategy_id,)
        )

    # ------------------------------------------------------------------
    # account_balances
    # ------------------------------------------------------------------
    async def save_balances(self, balances: dict[str, Balance]) -> None:
        """Atomically upsert all account balances."""
        now = time.time()
        async with self.transaction() as conn:
            await conn.executemany(
                "INSERT INTO account_balances (currency, free, reserved, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(currency) DO UPDATE SET free=excluded.free, "
                "reserved=excluded.reserved, updated_at=excluded.updated_at",
                [
                    (ccy, b.available, b.total - b.available, now)
                    for ccy, b in balances.items()
                ],
            )

    async def load_balances(self) -> dict[str, Balance]:
        """Load all persisted balances keyed by currency."""
        async with self.conn.execute("SELECT * FROM account_balances") as cursor:
            rows = await cursor.fetchall()
        result: dict[str, Balance] = {}
        for row in rows:
            free = float(row["free"])
            reserved = float(row["reserved"])
            result[str(row["currency"])] = Balance(
                total=free + reserved, available=free
            )
        return result

    # ------------------------------------------------------------------
    # equity_history
    # ------------------------------------------------------------------
    async def record_equity(
        self,
        total_equity: float,
        realized_pnl: float,
        unrealized_pnl: float,
        timestamp: float | None = None,
    ) -> None:
        """Append one equity snapshot to the history."""
        await self.conn.execute(
            "INSERT INTO equity_history (timestamp, total_equity, realized_pnl, "
            "unrealized_pnl) VALUES (?, ?, ?, ?)",
            (timestamp if timestamp is not None else time.time(),
             total_equity, realized_pnl, unrealized_pnl),
        )

    async def load_equity_history(
        self, limit: int = 1000
    ) -> list[tuple[float, float, float, float]]:
        """Load up to ``limit`` most recent snapshots, oldest first.

        Returns tuples of ``(timestamp, total_equity, realized_pnl,
        unrealized_pnl)``.
        """
        async with self.conn.execute(
            "SELECT timestamp, total_equity, realized_pnl, unrealized_pnl "
            "FROM equity_history ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            (float(r["timestamp"]), float(r["total_equity"]),
             float(r["realized_pnl"]), float(r["unrealized_pnl"]))
            for r in reversed(rows)
        ]
