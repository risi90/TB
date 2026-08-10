"""Paper trading engine: simulates order execution against live market data.

The engine keeps a full simulated account: per-currency balances (total and
available), open/closed orders, per-symbol positions, and realized/unrealized
PnL. Orders are matched against the best bid/ask observed from real-time
WebSocket tickers:

* **Market orders** fill immediately at the current ask (buy) or bid (sell)
  and pay the taker fee.
* **Limit orders** that cross the book at placement fill immediately as taker;
  otherwise they rest and are matched on every incoming ticker, paying the
  maker fee when they fill at their limit price.

Funds are reserved when an order is placed (quote for buys including the
worst-case fee, base for sells) and released on fill or cancel, so the
simulated account can never spend the same money twice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger

from engine.models import (
    Fill,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Ticker,
)


class PaperEngineError(Exception):
    """Base error for paper engine failures."""


class InsufficientFunds(PaperEngineError):
    """Raised when an order cannot be funded from the available balance."""


class UnknownMarketPrice(PaperEngineError):
    """Raised when a market order arrives before any ticker for its symbol."""


@dataclass(slots=True)
class Balance:
    """Total and available funds for one currency."""

    total: float = 0.0
    available: float = 0.0

    def reserve(self, amount: float) -> None:
        if amount > self.available + 1e-9:
            raise InsufficientFunds(
                f"tried to reserve {amount:.8f} with only {self.available:.8f} available"
            )
        self.available -= amount

    def release(self, amount: float) -> None:
        self.available = min(self.available + amount, self.total)


@dataclass(slots=True)
class _Reservation:
    """Funds locked for one open order."""

    currency: str
    amount: float = 0.0


def split_symbol(symbol: str) -> tuple[str, str]:
    """Split ``"BTC/EUR"`` into ``("BTC", "EUR")``."""
    base, _, quote = symbol.partition("/")
    if not base or not quote:
        raise ValueError(f"invalid symbol {symbol!r}, expected 'BASE/QUOTE'")
    return base, quote


@dataclass
class PaperEngine:
    """Simulated exchange account and matching engine.

    Args:
        balances: Initial funds per currency, e.g. ``{"EUR": 10_000.0}``.
        maker_fee_rate: Fee fraction charged on resting limit fills.
        taker_fee_rate: Fee fraction charged on market / crossing fills.
    """

    balances: dict[str, Balance]
    maker_fee_rate: float = 0.0015
    taker_fee_rate: float = 0.0025

    open_orders: dict[str, Order] = field(default_factory=dict)
    closed_orders: list[Order] = field(default_factory=list)
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)

    _last_tickers: dict[str, Ticker] = field(default_factory=dict)
    _reservations: dict[str, _Reservation] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        starting_balances: dict[str, float],
        maker_fee_rate: float = 0.0015,
        taker_fee_rate: float = 0.0025,
    ) -> "PaperEngine":
        """Build an engine from plain ``{currency: amount}`` starting funds."""
        return cls(
            balances={
                ccy: Balance(total=amt, available=amt)
                for ccy, amt in starting_balances.items()
            },
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
        )

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def process_ticker(self, ticker: Ticker) -> list[Fill]:
        """Ingest a live ticker and match resting limit orders against it.

        Returns the list of fills triggered by this update.
        """
        self._last_tickers[ticker.symbol] = ticker
        fills: list[Fill] = []
        for order in list(self.open_orders.values()):
            if order.symbol != ticker.symbol or order.type is not OrderType.LIMIT:
                continue
            assert order.price is not None
            if order.side is OrderSide.BUY and ticker.ask <= order.price:
                fills.append(self._fill_order(order, order.price, self.maker_fee_rate))
            elif order.side is OrderSide.SELL and ticker.bid >= order.price:
                fills.append(self._fill_order(order, order.price, self.maker_fee_rate))
        return fills

    def last_ticker(self, symbol: str) -> Ticker | None:
        """Most recent ticker seen for ``symbol``, if any."""
        return self._last_tickers.get(symbol)

    # ------------------------------------------------------------------
    # Order lifecycle
    # ------------------------------------------------------------------
    def create_order(self, request: OrderRequest) -> tuple[Order, list[Fill]]:
        """Place a simulated order.

        Funds are reserved up front; market orders (and crossing limit orders)
        fill immediately against the last known ticker.

        Returns:
            The tracked order and any immediate fills.

        Raises:
            InsufficientFunds: If the available balance cannot cover the order.
            UnknownMarketPrice: If a market order arrives before any ticker.
        """
        order = Order.from_request(request)
        ticker = self._last_tickers.get(order.symbol)

        if order.type is OrderType.MARKET and ticker is None:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "no market data yet for symbol"
            self.closed_orders.append(order)
            raise UnknownMarketPrice(f"no ticker seen yet for {order.symbol}")
        if order.type is OrderType.LIMIT and (order.price is None or order.price <= 0):
            order.status = OrderStatus.REJECTED
            order.reject_reason = "limit order requires a positive price"
            self.closed_orders.append(order)
            raise PaperEngineError("limit order requires a positive price")

        self._reserve_funds(order, ticker)
        self.open_orders[order.id] = order

        fills: list[Fill] = []
        if order.type is OrderType.MARKET:
            assert ticker is not None
            price = ticker.ask if order.side is OrderSide.BUY else ticker.bid
            fills.append(self._fill_order(order, price, self.taker_fee_rate))
        elif ticker is not None and order.price is not None:
            # A limit order that crosses the current book executes as taker.
            if order.side is OrderSide.BUY and order.price >= ticker.ask:
                fills.append(self._fill_order(order, ticker.ask, self.taker_fee_rate))
            elif order.side is OrderSide.SELL and order.price <= ticker.bid:
                fills.append(self._fill_order(order, ticker.bid, self.taker_fee_rate))

        return order, fills

    def cancel_order(self, order_id: str) -> Order:
        """Cancel an open order and release its reserved funds."""
        order = self.open_orders.pop(order_id, None)
        if order is None:
            raise PaperEngineError(f"order {order_id} is not open")
        self._release_reservation(order.id)
        order.status = OrderStatus.CANCELED
        order.updated_at = time.time()
        self.closed_orders.append(order)
        logger.info("Canceled paper order {} {} {}", order.side, order.symbol, order.id)
        return order

    def open_orders_for(self, symbol: str) -> list[Order]:
        """All open orders for one symbol."""
        return [o for o in self.open_orders.values() if o.symbol == symbol]

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------
    def equity(self) -> float:
        """Total account value in quote currency, marking positions at last price.

        Assumes a single quote currency across all traded symbols (the common
        setup, e.g. everything quoted in EUR). Currencies without a known
        ticker are valued at zero and logged once via debug.
        """
        total = 0.0
        for currency, balance in self.balances.items():
            price = self._currency_price(currency)
            if price is None:
                logger.debug("No price known for {}; excluded from equity", currency)
                continue
            total += balance.total * price
        return total

    def realized_pnl(self) -> float:
        """Sum of realized PnL across all positions (fees not deducted)."""
        return sum(p.realized_pnl for p in self.positions.values())

    def unrealized_pnl(self) -> float:
        """Sum of mark-to-market PnL across all open positions."""
        total = 0.0
        for symbol, position in self.positions.items():
            ticker = self._last_tickers.get(symbol)
            if ticker is not None:
                total += position.unrealized_pnl(ticker.last)
        return total

    def total_fees_paid(self) -> float:
        """All trading fees paid so far, in quote currency."""
        return sum(f.fee for f in self.fills)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _currency_price(self, currency: str) -> float | None:
        """Price of one unit of ``currency`` in the account's quote currency."""
        for symbol, ticker in self._last_tickers.items():
            base, quote = split_symbol(symbol)
            if currency == quote:
                return 1.0
            if currency == base:
                return ticker.last
        # No tickers at all yet: value bare quote balances at par.
        return 1.0 if not self._last_tickers else None

    def _balance(self, currency: str) -> Balance:
        return self.balances.setdefault(currency, Balance())

    def _reserve_funds(self, order: Order, ticker: Ticker | None) -> None:
        base, quote = split_symbol(order.symbol)
        if order.side is OrderSide.BUY:
            if order.type is OrderType.LIMIT and order.price is not None:
                reference = order.price
            else:
                assert ticker is not None
                reference = ticker.ask
            required = order.amount * reference * (1.0 + self.taker_fee_rate)
            self._balance(quote).reserve(required)
            self._reservations[order.id] = _Reservation(currency=quote, amount=required)
        else:
            self._balance(base).reserve(order.amount)
            self._reservations[order.id] = _Reservation(currency=base, amount=order.amount)

    def _release_reservation(self, order_id: str) -> None:
        reservation = self._reservations.pop(order_id, None)
        if reservation is not None and reservation.amount > 0:
            self._balance(reservation.currency).release(reservation.amount)

    def _fill_order(self, order: Order, price: float, fee_rate: float) -> Fill:
        """Fully fill ``order`` at ``price`` and settle balances and position."""
        base, quote = split_symbol(order.symbol)
        amount = order.amount
        cost = amount * price
        fee = cost * fee_rate

        # Release the reservation, then move real funds.
        self._release_reservation(order.id)
        base_balance = self._balance(base)
        quote_balance = self._balance(quote)

        if order.side is OrderSide.BUY:
            quote_balance.total -= cost + fee
            quote_balance.available -= cost + fee
            base_balance.total += amount
            base_balance.available += amount
        else:
            base_balance.total -= amount
            base_balance.available -= amount
            quote_balance.total += cost - fee
            quote_balance.available += cost - fee

        fill = Fill(
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            amount=amount,
            price=price,
            fee=fee,
        )
        self.fills.append(fill)

        position = self.positions.setdefault(order.symbol, Position(symbol=order.symbol))
        if order.side is OrderSide.BUY:
            position.apply_buy(amount, price)
            if order.stop_loss is not None:
                position.stop_loss = order.stop_loss
            if order.take_profit is not None:
                position.take_profit = order.take_profit
        else:
            realized = position.apply_sell(amount, price)
            logger.info(
                "Realized PnL {:+.2f} {} on {} (sold {:.8f} @ {:.2f})",
                realized, quote, order.symbol, amount, price,
            )

        order.filled = amount
        order.average_price = price
        order.fee_paid += fee
        order.status = OrderStatus.FILLED
        order.updated_at = time.time()
        self.open_orders.pop(order.id, None)
        self.closed_orders.append(order)

        logger.info(
            "FILL {} {} {:.8f} {} @ {:.2f} (fee {:.4f} {})",
            order.side.upper(), order.type, amount, order.symbol, price, fee, quote,
        )
        return fill
