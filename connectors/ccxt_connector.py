"""CCXT Pro connector for Bitvavo and Kraken.

Wraps ``ccxt.pro`` async exchange classes behind the
:class:`~connectors.base.BaseConnector` interface. The watch loops handle:

* network disconnections (``NetworkError``) — reconnect with exponential
  backoff and jitter,
* rate limiting (``RateLimitExceeded`` / HTTP 429) — back off harder,
* unexpected errors — logged and retried rather than crashing the bot.

Public market data streams (tickers / order books) work without API keys, so
paper trading against live data needs no credentials at all.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import ccxt.pro as ccxtpro
from ccxt.base.errors import (
    AuthenticationError,
    ExchangeNotAvailable,
    NetworkError,
    RateLimitExceeded,
)
from loguru import logger

from config.config import Settings
from connectors.base import BaseConnector, OrderBookHandler, TickerHandler
from engine.models import Order, OrderBook, OrderRequest, OrderStatus, Ticker
from utils.resilience import ExponentialBackoff

_SUPPORTED_EXCHANGES = ("bitvavo", "kraken")
_RATE_LIMIT_EXTRA_DELAY = 5.0


class CCXTProConnector(BaseConnector):
    """Async connector for a single exchange via ``ccxt.pro``.

    Args:
        exchange_id: One of ``"bitvavo"`` or ``"kraken"``.
        api_key: API key; only needed for private endpoints (live trading).
        api_secret: API secret; only needed for private endpoints.
    """

    def __init__(self, exchange_id: str, api_key: str = "", api_secret: str = "") -> None:
        if exchange_id not in _SUPPORTED_EXCHANGES:
            raise ValueError(
                f"unsupported exchange {exchange_id!r}; expected one of {_SUPPORTED_EXCHANGES}"
            )
        exchange_cls = getattr(ccxtpro, exchange_id)
        # aiohttp_trust_env makes REST requests honor standard HTTP(S)_PROXY
        # environment variables; a no-op when no proxy is configured.
        config: dict[str, Any] = {"enableRateLimit": True, "aiohttp_trust_env": True}
        if api_key and api_secret:
            config["apiKey"] = api_key
            config["secret"] = api_secret
        self._exchange = exchange_cls(config)
        ws_proxy = os.environ.get("WSS_PROXY") or os.environ.get("HTTPS_PROXY")
        if ws_proxy:
            self._exchange.ws_proxy = ws_proxy
        self._closed = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "CCXTProConnector":
        """Build a connector from application settings."""
        return cls(
            exchange_id=settings.exchange,
            api_key=settings.api_key,
            api_secret=settings.api_secret,
        )

    @property
    def exchange_id(self) -> str:
        """The CCXT exchange id this connector talks to."""
        return str(self._exchange.id)

    # ------------------------------------------------------------------
    # Market data streams
    # ------------------------------------------------------------------
    async def watch_ticker(self, symbol: str, handler: TickerHandler) -> None:
        """Stream best bid/ask tickers with automatic reconnection."""
        backoff = ExponentialBackoff(base=1.0, max_delay=60.0)
        while not self._closed:
            try:
                raw = await self._exchange.watch_ticker(symbol)
                backoff.reset()
                ticker = self._parse_ticker(symbol, raw)
                if ticker is not None:
                    await handler(ticker)
            except RateLimitExceeded as exc:
                delay = backoff.next_delay() + _RATE_LIMIT_EXTRA_DELAY
                logger.warning(
                    "Rate limited on {} ticker stream ({}); pausing {:.1f}s",
                    symbol, exc, delay,
                )
                await self._sleep(delay)
            except (NetworkError, ExchangeNotAvailable) as exc:
                delay = backoff.next_delay()
                logger.warning(
                    "WebSocket error on {} ticker stream (attempt {}): {}; "
                    "reconnecting in {:.1f}s",
                    symbol, backoff.attempt, exc, delay,
                )
                await self._sleep(delay)
            except Exception as exc:  # keep the stream alive on parser surprises
                delay = backoff.next_delay()
                logger.exception(
                    "Unexpected error on {} ticker stream: {}; retrying in {:.1f}s",
                    symbol, exc, delay,
                )
                await self._sleep(delay)

    async def watch_order_book(self, symbol: str, handler: OrderBookHandler) -> None:
        """Stream order book snapshots with automatic reconnection."""
        backoff = ExponentialBackoff(base=1.0, max_delay=60.0)
        while not self._closed:
            try:
                raw = await self._exchange.watch_order_book(symbol)
                backoff.reset()
                await handler(
                    OrderBook(
                        symbol=symbol,
                        bids=[(float(p), float(a)) for p, a, *_ in raw.get("bids", [])],
                        asks=[(float(p), float(a)) for p, a, *_ in raw.get("asks", [])],
                    )
                )
            except RateLimitExceeded as exc:
                delay = backoff.next_delay() + _RATE_LIMIT_EXTRA_DELAY
                logger.warning(
                    "Rate limited on {} order book stream ({}); pausing {:.1f}s",
                    symbol, exc, delay,
                )
                await self._sleep(delay)
            except (NetworkError, ExchangeNotAvailable) as exc:
                delay = backoff.next_delay()
                logger.warning(
                    "WebSocket error on {} order book stream (attempt {}): {}; "
                    "reconnecting in {:.1f}s",
                    symbol, backoff.attempt, exc, delay,
                )
                await self._sleep(delay)
            except Exception as exc:
                delay = backoff.next_delay()
                logger.exception(
                    "Unexpected error on {} order book stream: {}; retrying in {:.1f}s",
                    symbol, exc, delay,
                )
                await self._sleep(delay)

    # ------------------------------------------------------------------
    # Trading (live mode only)
    # ------------------------------------------------------------------
    async def create_order(self, request: OrderRequest) -> Order:
        """Place a real order via CCXT and mirror it into an :class:`Order`."""
        try:
            raw = await self._exchange.create_order(
                symbol=request.symbol,
                type=request.type.value,
                side=request.side.value,
                amount=request.amount,
                price=request.price,
            )
        except AuthenticationError as exc:
            logger.error("Authentication failed placing live order: {}", exc)
            raise
        order = Order.from_request(request)
        order.id = str(raw.get("id", order.id))
        status = str(raw.get("status") or "open")
        order.status = {
            "open": OrderStatus.OPEN,
            "closed": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELED,
            "rejected": OrderStatus.REJECTED,
        }.get(status, OrderStatus.OPEN)
        order.filled = float(raw.get("filled") or 0.0)
        order.average_price = float(raw.get("average") or 0.0)
        return order

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        """Cancel a real order via CCXT."""
        await self._exchange.cancel_order(order_id, symbol)

    async def fetch_balance(self) -> dict[str, float]:
        """Fetch free balances per currency (requires API keys)."""
        raw = await self._exchange.fetch_balance()
        free = raw.get("free", {}) or {}
        return {ccy: float(amt) for ccy, amt in free.items() if amt}

    async def close(self) -> None:
        """Close WebSocket and HTTP sessions."""
        self._closed = True
        await self._exchange.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_ticker(symbol: str, raw: dict[str, Any]) -> Ticker | None:
        """Convert a CCXT ticker dict into a :class:`Ticker`, if complete."""
        bid, ask = raw.get("bid"), raw.get("ask")
        last = raw.get("last") or raw.get("close")
        if bid is None or ask is None:
            return None
        if last is None:
            last = (float(bid) + float(ask)) / 2.0
        return Ticker(symbol=symbol, bid=float(bid), ask=float(ask), last=float(last))

    @staticmethod
    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)
