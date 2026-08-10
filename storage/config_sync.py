"""Runtime configuration synchronisation between SQLite and ``Settings``.

The Streamlit dashboard (or any other tool) edits rows in the ``bot_config``
table; the bot polls the table and applies changes to the live
:class:`~config.config.Settings` object without a process restart.

Precedence: on startup, :meth:`ConfigSync.seed` writes the current settings
into ``bot_config`` **only for keys that don't exist yet** — so values saved
from the dashboard survive bot restarts and override ``.env`` for the managed
keys below. Invalid values (failing pydantic validation) are logged and
skipped, leaving the last good value active.

Managed keys: ``symbols``, ``exchange``, ``grid_levels``,
``grid_spacing_pct``, ``grid_order_quote_size``, ``stop_loss_pct``,
``take_profit_pct``, ``max_allocation_pct``, ``max_drawdown_pct`` — plus the
special ``bot_status`` flag (``running`` / ``paused`` / ``stopped``) which is
not a settings field but drives the trading loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from pydantic import ValidationError

from config.config import Settings
from storage.db import Database

#: bot_config key -> Settings attribute name (identical for all managed keys).
MANAGED_KEYS: tuple[str, ...] = (
    "symbols",
    "exchange",
    "grid_levels",
    "grid_spacing_pct",
    "grid_order_quote_size",
    "stop_loss_pct",
    "take_profit_pct",
    "max_allocation_pct",
    "max_drawdown_pct",
)

#: Valid values for the bot_status flag.
BOT_STATUSES: tuple[str, ...] = ("running", "paused", "stopped")


@dataclass(slots=True)
class ConfigChange:
    """One applied runtime configuration change."""

    key: str
    old: str | None
    new: str


class ConfigSync:
    """Polls ``bot_config`` and applies validated changes to live settings."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._last_seen: dict[str, str] = {}
        self._bot_status: str = "running"

    @property
    def bot_status(self) -> str:
        """Last observed ``bot_status`` value (``running``/``paused``/``stopped``)."""
        return self._bot_status

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------
    def _serialize(self, key: str) -> str:
        value = getattr(self._settings, key)
        if key == "symbols":
            return ",".join(value)
        return str(value)

    @staticmethod
    def _parse(key: str, raw: str) -> Any:
        if key == "symbols":
            return [s.strip() for s in raw.split(",") if s.strip()]
        return raw  # pydantic coerces str -> int/float/Literal on assignment

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def seed(self) -> None:
        """Write current settings into ``bot_config`` for missing keys only.

        Existing rows (e.g. saved from the dashboard before a restart) are
        preserved; the following :meth:`poll` applies them to the settings.
        """
        defaults = {key: self._serialize(key) for key in MANAGED_KEYS}
        defaults["bot_status"] = "running"
        await self._db.seed_config(defaults)
        # Baseline = our own serialized settings, so the first poll reports
        # exactly the keys where the database differs from the process config.
        self._last_seen = {key: self._serialize(key) for key in MANAGED_KEYS}

    async def set_bot_status(self, status: str) -> None:
        """Write a new ``bot_status`` value (also updates the local cache)."""
        if status not in BOT_STATUSES:
            raise ValueError(f"invalid bot_status {status!r}")
        await self._db.set_config("bot_status", status)
        self._bot_status = status

    async def poll(self) -> list[ConfigChange]:
        """Read ``bot_config`` and apply any changed managed keys.

        Returns:
            The list of changes applied this round (including ``bot_status``
            transitions). Invalid values are logged, skipped, and not
            reported again until they change in the database.
        """
        raw = await self._db.get_all_config()
        changes: list[ConfigChange] = []

        for key in MANAGED_KEYS:
            if key not in raw:
                continue
            new_raw = raw[key]
            old_raw = self._last_seen.get(key)
            if new_raw == old_raw:
                continue
            # Remember the raw value first so a bad value doesn't log forever.
            self._last_seen[key] = new_raw
            try:
                setattr(self._settings, key, self._parse(key, new_raw))
            except ValidationError as exc:
                logger.warning(
                    "Ignoring invalid runtime config {}={!r}: {}",
                    key, new_raw, exc.errors()[0].get("msg", exc),
                )
                continue
            logger.info("Runtime config applied: {} = {} (was {})", key, new_raw, old_raw)
            changes.append(ConfigChange(key=key, old=old_raw, new=new_raw))

        status = raw.get("bot_status", self._bot_status)
        if status not in BOT_STATUSES:
            logger.warning("Ignoring invalid bot_status {!r} in database", status)
        elif status != self._bot_status:
            changes.append(
                ConfigChange(key="bot_status", old=self._bot_status, new=status)
            )
            self._bot_status = status

        return changes
