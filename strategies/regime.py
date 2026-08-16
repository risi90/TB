"""Market regime detection for gating strategy entries.

Uses Kaufman's Efficiency Ratio (ER) over a rolling window of bar closes:

    ER = |close_now − close_window_ago| / Σ |close_i − close_i−1|

ER approaches 1 when price moves cleanly in one direction (trending) and 0
when it churns without going anywhere (ranging). Long-only grid strategies
bleed in downtrends — they keep buying into falling prices — so the grid
uses this filter to stop opening new entries while the regime is
``TRENDING_DOWN``.

Hysteresis prevents flapping: the regime becomes trending when ER rises to
``enter_threshold`` and only returns to ranging when ER falls back below
``exit_threshold``.

The filter is bar-based (feed it completed bar closes, never raw ticks) so
live behavior matches backtests — set ``BAR_TIMEFRAME`` to the timeframe
you validated against. State is not persisted: after a restart the filter
reports ``RANGING`` until its window refills.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum


class Regime(StrEnum):
    """Detected market regime."""

    RANGING = "ranging"
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"


class RegimeFilter:
    """Efficiency-ratio trend detector with hysteresis.

    Args:
        window: Number of bars the ratio looks back over.
        enter_threshold: ER at/above which the regime becomes trending.
        exit_threshold: ER at/below which a trending regime ends; must be
            below ``enter_threshold``.
    """

    def __init__(
        self,
        window: int = 48,
        enter_threshold: float = 0.35,
        exit_threshold: float = 0.25,
    ) -> None:
        if window < 2:
            raise ValueError("window must be at least 2")
        if not 0 < exit_threshold < enter_threshold <= 1:
            raise ValueError("need 0 < exit_threshold < enter_threshold <= 1")
        self._window = window
        self._enter = enter_threshold
        self._exit = exit_threshold
        self._closes: deque[float] = deque(maxlen=window + 1)
        self._er = 0.0
        self._regime = Regime.RANGING

    @property
    def regime(self) -> Regime:
        """The current regime (``RANGING`` until the window is full)."""
        return self._regime

    @property
    def efficiency_ratio(self) -> float:
        """Latest computed ER (0.0 until the window is full)."""
        return self._er

    @property
    def warmed_up(self) -> bool:
        """Whether the lookback window is full."""
        return len(self._closes) == self._closes.maxlen

    def update(self, close: float) -> Regime:
        """Ingest one completed bar close and return the resulting regime."""
        self._closes.append(close)
        if not self.warmed_up:
            return self._regime

        net = self._closes[-1] - self._closes[0]
        closes = list(self._closes)
        path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
        self._er = abs(net) / path if path > 0 else 0.0

        if self._er >= self._enter:
            self._regime = Regime.TRENDING_UP if net > 0 else Regime.TRENDING_DOWN
        elif self._er <= self._exit:
            self._regime = Regime.RANGING
        # Between the thresholds the previous regime persists (hysteresis).
        return self._regime
