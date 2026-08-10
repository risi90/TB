"""Risk management: per-trade allocation caps, drawdown limits, and SL/TP.

Every order a strategy generates MUST pass through
:meth:`RiskManager.validate_order` before it reaches the execution router.
The manager:

* rejects entries whose notional exceeds ``max_allocation_pct`` of equity,
* rejects entries below the minimum notional or beyond the open-order cap,
* blocks all new entries while portfolio drawdown from peak equity exceeds
  ``max_drawdown_pct`` (risk-reducing sells always remain allowed),
* stamps mandatory stop-loss / take-profit levels on every entry that does
  not already define them.

It also provides :meth:`check_protective_exit` which the main loop calls on
every ticker to enforce stop-loss / take-profit exits on open positions.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from config.config import Settings
from engine.models import OrderRequest, OrderSide, OrderType, Position, Ticker


@dataclass(slots=True)
class RiskDecision:
    """Outcome of a risk check for one order request."""

    approved: bool
    request: OrderRequest
    reason: str | None = None


class RiskManager:
    """Stateful risk gatekeeper for all strategy-generated orders."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._peak_equity: float = 0.0
        self._current_equity: float = 0.0

    # ------------------------------------------------------------------
    # Equity / drawdown tracking
    # ------------------------------------------------------------------
    def update_equity(self, equity: float) -> None:
        """Record the latest portfolio equity and update the peak."""
        self._current_equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity

    @property
    def drawdown(self) -> float:
        """Current drawdown from peak equity as a fraction (0.0 – 1.0)."""
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, 1.0 - self._current_equity / self._peak_equity)

    @property
    def drawdown_breached(self) -> bool:
        """Whether the max-drawdown circuit breaker is currently tripped."""
        return self.drawdown >= self._settings.max_drawdown_pct

    # ------------------------------------------------------------------
    # Order validation
    # ------------------------------------------------------------------
    def validate_order(
        self,
        request: OrderRequest,
        reference_price: float,
        open_order_count: int = 0,
    ) -> RiskDecision:
        """Vet one order request against all configured risk limits.

        Args:
            request: The strategy's proposed order.
            reference_price: Current market price used to value the order.
            open_order_count: Open orders already resting for this symbol.

        Returns:
            A :class:`RiskDecision`; when approved, the embedded request has
            stop-loss / take-profit levels stamped on entries.
        """
        is_entry = request.side is OrderSide.BUY and not request.reduce_only

        if request.amount <= 0:
            return self._reject(request, "order amount must be positive")

        notional = request.notional(reference_price)

        if is_entry:
            if self.drawdown_breached:
                return self._reject(
                    request,
                    f"drawdown {self.drawdown:.1%} exceeds limit "
                    f"{self._settings.max_drawdown_pct:.1%}; new entries blocked",
                )
            if notional < self._settings.min_order_notional:
                return self._reject(
                    request,
                    f"notional {notional:.2f} below minimum "
                    f"{self._settings.min_order_notional:.2f}",
                )
            max_notional = self._settings.max_allocation_pct * self._current_equity
            if self._current_equity > 0 and notional > max_notional:
                return self._reject(
                    request,
                    f"notional {notional:.2f} exceeds max allocation "
                    f"{max_notional:.2f} ({self._settings.max_allocation_pct:.0%} of equity)",
                )
            if open_order_count >= self._settings.max_open_orders:
                return self._reject(
                    request,
                    f"open order cap reached ({self._settings.max_open_orders})",
                )
            self._apply_protective_levels(request, reference_price)

        return RiskDecision(approved=True, request=request)

    def _apply_protective_levels(
        self, request: OrderRequest, reference_price: float
    ) -> None:
        """Stamp mandatory SL/TP prices on an entry if the strategy left them unset."""
        entry_price = (
            request.price
            if request.type is OrderType.LIMIT and request.price
            else reference_price
        )
        if request.stop_loss is None:
            request.stop_loss = entry_price * (1.0 - self._settings.stop_loss_pct)
        if request.take_profit is None:
            request.take_profit = entry_price * (1.0 + self._settings.take_profit_pct)

    def _reject(self, request: OrderRequest, reason: str) -> RiskDecision:
        logger.warning(
            "Risk REJECTED {} {} {}: {}",
            request.side.upper(), request.amount, request.symbol, reason,
        )
        return RiskDecision(approved=False, request=request, reason=reason)

    # ------------------------------------------------------------------
    # Protective exits
    # ------------------------------------------------------------------
    def check_protective_exit(
        self, position: Position, ticker: Ticker
    ) -> OrderRequest | None:
        """Return a market-sell request if the position breached its SL or TP.

        Called by the main loop on every ticker for symbols with an open
        position; enforcement is therefore independent of the strategy.
        """
        if position.amount <= 0:
            return None
        if position.stop_loss is not None and ticker.bid <= position.stop_loss:
            logger.warning(
                "STOP-LOSS hit on {} (bid {:.2f} <= SL {:.2f})",
                position.symbol, ticker.bid, position.stop_loss,
            )
        elif position.take_profit is not None and ticker.bid >= position.take_profit:
            logger.info(
                "TAKE-PROFIT hit on {} (bid {:.2f} >= TP {:.2f})",
                position.symbol, ticker.bid, position.take_profit,
            )
        else:
            return None
        return OrderRequest(
            symbol=position.symbol,
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            amount=position.amount,
            reduce_only=True,
        )
