"""Bridges the in-memory :class:`~engine.paper_engine.PaperEngine` and SQLite.

The paper engine itself stays a pure, synchronous, fully-testable matching
engine; this module owns all database I/O around it:

* :meth:`PersistenceManager.hydrate_engine` restores balances, positions and
  open orders on startup (a no-op on a fresh database, where the configured
  starting balances apply).
* :meth:`PersistenceManager.persist_account` writes balances + positions in
  one atomic transaction after fills or cancels.
* :meth:`PersistenceManager.persist_order` mirrors individual order
  transitions (placed, filled, canceled) into the ``orders`` table.
* :meth:`PersistenceManager.snapshot_equity` appends to ``equity_history``.

Once a database exists, its balances take precedence over the configured
``PAPER_STARTING_BALANCE`` so the account survives restarts.
"""

from __future__ import annotations

from loguru import logger

from engine.models import Order
from engine.paper_engine import PaperEngine
from storage.db import Database


class PersistenceManager:
    """All persistence orchestration for one paper engine instance."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def hydrate_engine(self, engine: PaperEngine) -> dict[str, str]:
        """Restore a persisted account snapshot into ``engine``, if one exists.

        Returns:
            Mapping of restored open-order ids to their owning strategy ids
            (empty when the database is fresh and nothing was restored).
        """
        balances = await self._db.load_balances()
        if not balances:
            logger.info("No persisted state found — starting with configured balances")
            return {}

        positions = await self._db.load_positions()
        open_rows = await self._db.load_open_orders()
        engine.restore_state(
            balances=balances,
            positions=positions,
            open_orders=[order for order, _ in open_rows],
        )
        logger.info(
            "Restored state from {}: {} currencies, {} positions, {} open orders",
            self._db.path, len(balances), len(positions), len(open_rows),
        )
        return {
            order.id: strategy_id
            for order, strategy_id in open_rows
            if strategy_id is not None
        }

    async def persist_account(self, engine: PaperEngine) -> None:
        """Write balances and all positions in one atomic transaction."""
        await self._db.save_balances(engine.balances)
        for position in engine.positions.values():
            await self._db.upsert_position(position)

    async def persist_order(self, order: Order, strategy_id: str | None = None) -> None:
        """Mirror one order's current state into the ``orders`` table."""
        await self._db.upsert_order(order, strategy_id)

    async def persist_orders(
        self, orders: list[Order], strategy_ids: dict[str, str] | None = None
    ) -> None:
        """Mirror several orders, resolving strategy ids from ``strategy_ids``."""
        ids = strategy_ids or {}
        for order in orders:
            await self._db.upsert_order(order, ids.get(order.id))

    async def snapshot_equity(self, engine: PaperEngine) -> None:
        """Append the engine's current equity/PnL to ``equity_history``."""
        await self._db.record_equity(
            total_equity=engine.equity(),
            realized_pnl=engine.realized_pnl(),
            unrealized_pnl=engine.unrealized_pnl(),
        )
