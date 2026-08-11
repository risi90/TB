"""Tests for runtime configuration changes applied through the database."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from config.config import Settings
from storage.config_sync import ConfigSync
from storage.db import Database


@pytest.fixture()
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture()
async def sync(db: Database, settings: Settings) -> ConfigSync:
    config_sync = ConfigSync(db, settings)
    await config_sync.seed()
    return config_sync


async def test_seed_writes_managed_defaults(db: Database, sync: ConfigSync) -> None:
    stored = await db.get_all_config()
    assert stored["symbols"] == "BTC/EUR"
    assert stored["exchange"] == "bitvavo"
    assert stored["grid_levels"] == "5"
    assert stored["bot_status"] == "running"


async def test_poll_reports_nothing_when_unchanged(sync: ConfigSync) -> None:
    assert await sync.poll() == []


async def test_runtime_change_applied_to_settings(
    db: Database, settings: Settings, sync: ConfigSync
) -> None:
    await db.set_config("grid_spacing_pct", "0.01")
    await db.set_config("grid_levels", "7")
    changes = await sync.poll()

    assert {c.key for c in changes} == {"grid_spacing_pct", "grid_levels"}
    assert settings.grid_spacing_pct == pytest.approx(0.01)
    assert settings.grid_levels == 7  # coerced from str to int
    # Applied changes are not re-reported on the next poll.
    assert await sync.poll() == []


async def test_symbols_change_parsed_to_list(
    db: Database, settings: Settings, sync: ConfigSync
) -> None:
    await db.set_config("symbols", "BTC/EUR,ETH/EUR")
    changes = await sync.poll()
    assert [c.key for c in changes] == ["symbols"]
    assert settings.symbols == ["BTC/EUR", "ETH/EUR"]


async def test_risk_limit_change_applied(
    db: Database, settings: Settings, sync: ConfigSync
) -> None:
    await db.set_config("stop_loss_pct", "0.05")
    await db.set_config("max_allocation_pct", "0.25")
    await sync.poll()
    assert settings.stop_loss_pct == pytest.approx(0.05)
    assert settings.max_allocation_pct == pytest.approx(0.25)


async def test_invalid_value_ignored_and_not_applied(
    db: Database, settings: Settings, sync: ConfigSync
) -> None:
    before = settings.grid_levels
    await db.set_config("grid_levels", "not-a-number")
    changes = await sync.poll()
    assert changes == []
    assert settings.grid_levels == before
    # The bad value is remembered so it isn't re-reported every poll…
    assert await sync.poll() == []
    # …but a subsequent good value is applied.
    await db.set_config("grid_levels", "9")
    changes = await sync.poll()
    assert [c.key for c in changes] == ["grid_levels"]
    assert settings.grid_levels == 9


async def test_out_of_bounds_value_rejected(
    db: Database, settings: Settings, sync: ConfigSync
) -> None:
    await db.set_config("max_allocation_pct", "5.0")  # > 1.0 upper bound
    assert await sync.poll() == []
    assert settings.max_allocation_pct == pytest.approx(0.10)


async def test_bot_status_transitions_reported(
    db: Database, sync: ConfigSync
) -> None:
    assert sync.bot_status == "running"
    await db.set_config("bot_status", "paused")
    changes = await sync.poll()
    assert [(c.key, c.new) for c in changes] == [("bot_status", "paused")]
    assert sync.bot_status == "paused"

    await db.set_config("bot_status", "garbage")  # invalid -> ignored
    assert await sync.poll() == []
    assert sync.bot_status == "paused"

    await sync.set_bot_status("stopped")
    assert await db.get_config("bot_status") == "stopped"
    assert sync.bot_status == "stopped"


def test_symbols_parse_from_real_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain comma-separated SYMBOLS must work as an actual env var (Docker
    env_file), not only via .env files — pydantic-settings would otherwise
    JSON-decode the list field at the source level and crash."""
    monkeypatch.setenv("SYMBOLS", "BTC/EUR,ETH/EUR")
    settings = Settings(_env_file=None)
    assert settings.symbols == ["BTC/EUR", "ETH/EUR"]


# ---------------------------------------------------------------------------
# Fee floor enforcement
# ---------------------------------------------------------------------------
def test_grid_spacing_below_fee_floor_rejected_at_startup() -> None:
    # Floor with default 0.15% maker fee: 2 * 0.0015 + 0.001 = 0.004.
    with pytest.raises(ValidationError, match="fee floor"):
        Settings(_env_file=None, grid_spacing_pct=0.002)


def test_fee_floor_only_applies_to_grid_strategy() -> None:
    settings = Settings(
        _env_file=None, strategy="sma_crossover", grid_spacing_pct=0.002
    )
    assert settings.grid_spacing_pct == pytest.approx(0.002)
    assert Settings(_env_file=None).grid_fee_floor() == pytest.approx(0.004)


async def test_runtime_spacing_below_fee_floor_ignored(
    db: Database, settings: Settings, sync: ConfigSync
) -> None:
    before = settings.grid_spacing_pct
    await db.set_config("grid_spacing_pct", "0.001")  # below the 0.004 floor
    assert await sync.poll() == []
    assert settings.grid_spacing_pct == pytest.approx(before)
    # A compliant value is applied afterwards.
    await db.set_config("grid_spacing_pct", "0.008")
    changes = await sync.poll()
    assert [c.key for c in changes] == ["grid_spacing_pct"]
    assert settings.grid_spacing_pct == pytest.approx(0.008)


async def test_dashboard_settings_survive_restart(
    db: Database, settings: Settings
) -> None:
    """Values saved to the DB before a 'restart' override .env on the next boot."""
    await db.set_config("grid_levels", "12")  # dashboard edit while bot was down

    sync = ConfigSync(db, settings)  # simulated new process
    await sync.seed()  # must NOT overwrite the stored 12
    changes = await sync.poll()

    assert [c.key for c in changes] == ["grid_levels"]
    assert settings.grid_levels == 12
