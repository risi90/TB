"""Application configuration via pydantic-settings and ``.env`` files.

Safety model
------------
``paper_trading`` defaults to ``True``. Live trading is only possible when the
operator explicitly sets ``PAPER_TRADING=False`` *and* provides non-empty API
credentials. :meth:`Settings.live_trading_allowed` is the single source of
truth consulted by the execution router before any real order leaves the bot.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from engine.candle_aggregator import TIMEFRAMES_MS

SupportedExchange = Literal["bitvavo", "kraken"]
StrategyName = Literal["grid", "sma_crossover"]


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_assignment=True,
    )

    # --- Safety ---------------------------------------------------------
    paper_trading: bool = Field(
        default=True,
        description="Paper mode (default). Real orders are blocked while True.",
    )

    # --- Exchange -------------------------------------------------------
    exchange: SupportedExchange = "bitvavo"
    api_key: str = ""
    api_secret: str = ""

    # --- Market ---------------------------------------------------------
    # NoDecode: keep pydantic-settings from JSON-parsing the env value so
    # plain `SYMBOLS=BTC/EUR,ETH/EUR` works from real environment variables
    # (e.g. docker compose env_file) as well as from .env files.
    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTC/EUR"]
    )
    quote_currency: str = "EUR"

    # --- Paper trading --------------------------------------------------
    paper_starting_balance: float = Field(default=10_000.0, gt=0)
    maker_fee_rate: float = Field(default=0.0015, ge=0, lt=0.1)
    taker_fee_rate: float = Field(default=0.0025, ge=0, lt=0.1)

    # --- Strategy -------------------------------------------------------
    strategy: StrategyName = "grid"
    bar_timeframe: str = Field(
        default="1m",
        description="Timeframe of the live bars strategies compute indicators on.",
    )

    grid_levels: int = Field(default=5, ge=1, le=50)
    grid_spacing_pct: float = Field(default=0.005, gt=0, lt=0.5)
    grid_order_quote_size: float = Field(default=100.0, gt=0)
    grid_auto_reanchor: bool = True
    grid_reanchor_factor: float = Field(default=1.5, gt=0)
    grid_aligned_protection: bool = Field(
        default=True,
        description="Stop-loss below the whole grid instead of below each "
        "entry (per-entry stops turn normal grid dips into realized losses).",
    )
    grid_regime_filter: bool = Field(
        default=True,
        description="Suspend new grid entries while the market trends down "
        "(efficiency-ratio based); sells stay active.",
    )
    regime_window: int = Field(default=48, ge=5, le=500)
    regime_enter_threshold: float = Field(default=0.35, gt=0, le=1)
    regime_exit_threshold: float = Field(default=0.25, gt=0, le=1)
    grid_max_inventory_quote: float = Field(
        default=0.0, ge=0,
        description="Committed-capital cap in quote currency; 0 = one full grid.",
    )

    sma_fast_period: int = Field(default=10, ge=2)
    sma_slow_period: int = Field(default=30, ge=3)
    sma_order_quote_size: float = Field(default=250.0, gt=0)

    # --- Risk -----------------------------------------------------------
    max_allocation_pct: float = Field(default=0.10, gt=0, le=1)
    max_drawdown_pct: float = Field(default=0.15, gt=0, le=1)
    stop_loss_pct: float = Field(default=0.02, gt=0, lt=1)
    take_profit_pct: float = Field(default=0.04, gt=0, lt=1)
    max_open_orders: int = Field(default=10, ge=1)
    min_order_notional: float = Field(default=5.0, ge=0)

    # --- Dashboard ------------------------------------------------------
    dashboard_password: str = Field(
        default="",
        description="Password gate for the Streamlit dashboard; empty = no lock.",
    )

    # --- Notifications --------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    digest_interval_hours: float = Field(default=24.0, gt=0)

    # --- Persistence ----------------------------------------------------
    db_path: str = "data/bot_state.db"
    config_poll_interval: float = Field(default=5.0, gt=0)
    equity_snapshot_interval: float = Field(default=30.0, gt=0)
    heartbeat_interval: float = Field(default=5.0, gt=0)

    # --- Logging --------------------------------------------------------
    log_level: str = "INFO"
    log_file: str = "logs/bot.log"
    pnl_summary_interval: float = Field(default=30.0, gt=0)

    @field_validator("symbols", mode="before")
    @classmethod
    def _split_symbols(cls, value: object) -> object:
        """Allow ``SYMBOLS=BTC/EUR,ETH/EUR`` style comma-separated strings."""
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return value

    @field_validator("sma_slow_period")
    @classmethod
    def _slow_gt_fast(cls, value: int, info) -> int:
        fast = info.data.get("sma_fast_period")
        if fast is not None and value <= fast:
            raise ValueError("sma_slow_period must be greater than sma_fast_period")
        return value

    @field_validator("bar_timeframe")
    @classmethod
    def _known_timeframe(cls, value: str) -> str:
        if value not in TIMEFRAMES_MS:
            raise ValueError(
                f"bar_timeframe must be one of {sorted(TIMEFRAMES_MS)}"
            )
        return value

    def grid_fee_floor(self) -> float:
        """Minimum profitable grid spacing: a completed cycle pays roughly two
        maker fees, plus a 0.1% safety margin for slippage and spread."""
        return 2.0 * self.maker_fee_rate + 0.001

    @model_validator(mode="after")
    def _regime_thresholds_ordered(self) -> "Settings":
        if self.regime_exit_threshold >= self.regime_enter_threshold:
            raise ValueError(
                "regime_exit_threshold must be below regime_enter_threshold"
            )
        return self

    @model_validator(mode="after")
    def _grid_spacing_covers_fees(self) -> "Settings":
        """Block grid configurations that are structurally unprofitable.

        A grid cycle earns ``spacing`` and pays ~two maker fees; spacing at
        or below that guarantees every cycle loses money. Only enforced when
        the grid strategy is active. Re-checked on runtime assignment
        (``validate_assignment=True``), so dashboard edits are vetted too.
        """
        if self.strategy == "grid" and self.grid_spacing_pct < self.grid_fee_floor():
            raise ValueError(
                f"grid_spacing_pct {self.grid_spacing_pct:.4%} is below the fee "
                f"floor {self.grid_fee_floor():.4%} (2 x maker fee + 0.1% margin) "
                f"— every grid cycle would lose money"
            )
        return self

    def live_trading_allowed(self) -> bool:
        """Return ``True`` only when live trading is explicitly and safely enabled.

        Requires ``paper_trading`` to be disabled *and* both API credentials to
        be present. Every real-order code path must consult this method.
        """
        return (
            self.paper_trading is False
            and bool(self.api_key.strip())
            and bool(self.api_secret.strip())
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings` instance."""
    return Settings()
