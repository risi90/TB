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
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SupportedExchange = Literal["bitvavo", "kraken"]
StrategyName = Literal["grid", "sma_crossover"]


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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
    symbols: list[str] = Field(default_factory=lambda: ["BTC/EUR"])
    quote_currency: str = "EUR"

    # --- Paper trading --------------------------------------------------
    paper_starting_balance: float = Field(default=10_000.0, gt=0)
    maker_fee_rate: float = Field(default=0.0015, ge=0, lt=0.1)
    taker_fee_rate: float = Field(default=0.0025, ge=0, lt=0.1)

    # --- Strategy -------------------------------------------------------
    strategy: StrategyName = "grid"

    grid_levels: int = Field(default=5, ge=1, le=50)
    grid_spacing_pct: float = Field(default=0.005, gt=0, lt=0.5)
    grid_order_quote_size: float = Field(default=100.0, gt=0)

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

    # --- Logging --------------------------------------------------------
    log_level: str = "INFO"
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
