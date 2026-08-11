"""Shared domain models: market data, orders, fills, and positions."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class OrderSide(StrEnum):
    """Direction of an order."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """Execution style of an order."""

    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    """Lifecycle state of an order."""

    OPEN = "open"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass(slots=True)
class Ticker:
    """A best-bid/ask snapshot for one symbol."""

    symbol: str
    bid: float
    ask: float
    last: float
    timestamp: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        """Mid price between best bid and best ask."""
        return (self.bid + self.ask) / 2.0


@dataclass(slots=True)
class OrderBook:
    """A (partial) order book snapshot: lists of ``(price, amount)`` levels."""

    symbol: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    timestamp: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None


@dataclass(slots=True)
class OrderRequest:
    """An intent to trade, produced by a strategy and vetted by the risk manager.

    ``price`` is required for limit orders and ignored for market orders.
    ``stop_loss`` / ``take_profit`` are protective price levels; the risk
    manager fills them in for entries when the strategy leaves them unset.
    """

    symbol: str
    side: OrderSide
    type: OrderType
    amount: float
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reduce_only: bool = False

    def notional(self, reference_price: float) -> float:
        """Order value in quote currency at ``reference_price`` (or limit price)."""
        price = self.price if (self.type is OrderType.LIMIT and self.price) else reference_price
        return self.amount * price


@dataclass(slots=True)
class Fill:
    """A single execution of (part of) an order."""

    order_id: str
    symbol: str
    side: OrderSide
    amount: float
    price: float
    fee: float
    timestamp: float = field(default_factory=time.time)

    @property
    def cost(self) -> float:
        """Traded value in quote currency, excluding fees."""
        return self.amount * self.price


@dataclass(slots=True)
class Order:
    """A tracked order inside the paper engine (or mirrored from an exchange)."""

    symbol: str
    side: OrderSide
    type: OrderType
    amount: float
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reduce_only: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: OrderStatus = OrderStatus.OPEN
    filled: float = 0.0
    average_price: float = 0.0
    fee_paid: float = 0.0
    reject_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def is_open(self) -> bool:
        return self.status is OrderStatus.OPEN

    @classmethod
    def from_request(cls, request: OrderRequest) -> "Order":
        """Build a tracked order from a validated :class:`OrderRequest`."""
        return cls(
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            amount=request.amount,
            price=request.price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            reduce_only=request.reduce_only,
        )


@dataclass(slots=True)
class Position:
    """A long position in one symbol with running **net** PnL bookkeeping.

    ``realized_pnl`` is net of trading costs: each sale's gross PnL is
    reduced by the sell fee and a proportional share of the fees paid to
    build the position (``entry_fees``). Slippage is already embedded in
    fill prices. ``unrealized_pnl`` stays gross (mark-to-market before any
    exit costs).
    """

    symbol: str
    amount: float = 0.0
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    entry_fees: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None

    def unrealized_pnl(self, last_price: float) -> float:
        """Mark-to-market PnL of the open amount at ``last_price`` (gross)."""
        if self.amount <= 0:
            return 0.0
        return (last_price - self.entry_price) * self.amount

    def apply_buy(self, amount: float, price: float, fee: float = 0.0) -> None:
        """Increase the position, updating the weighted-average entry price
        and accumulating the entry fee for later net-PnL attribution."""
        total = self.amount + amount
        if total > 0:
            self.entry_price = (self.amount * self.entry_price + amount * price) / total
        self.amount = total
        self.entry_fees += fee

    def apply_sell(self, amount: float, price: float, fee: float = 0.0) -> float:
        """Reduce the position and return the **net** realized PnL of this sale.

        Net PnL = gross PnL − ``fee`` (the sell's own fee) − the
        proportional share of accumulated entry fees for the closed amount.
        """
        closed = min(amount, self.amount)
        gross = (price - self.entry_price) * closed
        entry_fee_share = (
            self.entry_fees * (closed / self.amount) if self.amount > 0 else 0.0
        )
        self.entry_fees -= entry_fee_share
        net = gross - entry_fee_share - fee
        self.realized_pnl += net
        self.amount -= closed
        if self.amount <= 1e-12:
            self.amount = 0.0
            self.entry_price = 0.0
            self.entry_fees = 0.0
            self.stop_loss = None
            self.take_profit = None
        return net
