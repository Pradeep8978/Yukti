"""
yukti/execution/dhan_client.py
Thin async wrapper around the dhanhq SDK.
Handles retries, rate limiting, and maps to DhanHQ constants.
"""
from __future__ import annotations

import asyncio
import logging
import time
from functools import wraps
from typing import Any, Callable

from dhanhq import dhanhq
from tenacity import retry, stop_after_attempt, wait_exponential

from yukti.config import settings

log = logging.getLogger(__name__)

# ── Token bucket rate limiter (20 req/sec DhanHQ limit) ──────────────────────

class _TokenBucket:
    def __init__(self, rate: float = 18.0) -> None:  # slightly under 20
        self._rate     = rate
        self._tokens   = rate
        self._last_ts  = time.monotonic()
        self._lock     = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_ts
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last_ts = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


_bucket = _TokenBucket()


def rate_limited(fn: Callable) -> Callable:
    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        await _bucket.acquire()
        return fn(*args, **kwargs)
    return wrapper


# ── DhanHQ client wrapper ─────────────────────────────────────────────────────

class DhanClient:
    """
    Async-friendly wrapper around the synchronous dhanhq SDK.
    All SDK calls run in a thread pool executor to avoid blocking the event loop.
    """

    def __init__(self) -> None:
        self._dhan = dhanhq(
            client_id    = settings.dhan_client_id,
            access_token = settings.dhan_access_token,
        )
        self._loop = asyncio.get_event_loop()

    async def _call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous SDK call in the thread pool + rate limiter."""
        await _bucket.acquire()
        return await self._loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── Orders ────────────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5), reraise=True)
    async def place_order(
        self,
        security_id:      str,
        transaction_type: str,      # "BUY" | "SELL"
        quantity:         int,
        order_type:       str,      # "LIMIT" | "MARKET" | "SL" | "SL-M"
        product_type:     str,      # "INTRADAY" | "DELIVERY"
        price:            float = 0.0,
        trigger_price:    float = 0.0,
        tag:              str   = "yukti",
    ) -> dict[str, Any]:
        result = await self._call(
            self._dhan.place_order,
            security_id      = security_id,
            exchange_segment = self._dhan.NSE,
            transaction_type = transaction_type,
            quantity         = quantity,
            order_type       = order_type,
            product_type     = product_type,
            price            = price,
            trigger_price    = trigger_price,
            validity         = "DAY",
            tag              = tag,
        )
        log.info("place_order %s %s qty=%d → %s", transaction_type, security_id, quantity, result)
        return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5), reraise=True)
    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        result = await self._call(self._dhan.cancel_order, order_id=order_id)
        log.info("cancel_order %s → %s", order_id, result)
        return result

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        return await self._call(self._dhan.get_order_by_id, order_id=order_id)

    # ── GTT orders ────────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5), reraise=True)
    async def place_gtt(
        self,
        security_id:      str,
        transaction_type: str,
        quantity:         int,
        trigger_price:    float,
        order_type:       str,
        product_type:     str,
        price:            float = 0.0,
    ) -> dict[str, Any]:
        result = await self._call(
            self._dhan.place_gtt_order,
            security_id      = security_id,
            exchange_segment = self._dhan.NSE,
            transaction_type = transaction_type,
            quantity         = quantity,
            trigger_price    = trigger_price,
            order_type       = order_type,
            product_type     = product_type,
            price            = price,
        )
        log.info("place_gtt trigger=%.2f %s qty=%d → %s", trigger_price, security_id, quantity, result)
        return result

    async def cancel_gtt(self, gtt_id: str) -> dict[str, Any]:
        return await self._call(self._dhan.cancel_gtt_order, order_id=gtt_id)

    # ── Positions ─────────────────────────────────────────────────────────────

    async def get_positions(self) -> list[dict[str, Any]]:
        result = await self._call(self._dhan.get_positions)
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_order_list(self) -> list[dict[str, Any]]:
        result = await self._call(self._dhan.get_order_list)
        return result.get("data", []) if isinstance(result, dict) else []

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_candles(
        self,
        security_id:  str,
        interval:     str = "5",
        from_date:    str = "",
        to_date:      str = "",
    ) -> list[dict[str, Any]]:
        """Fetch historical candles. Returns list of OHLCV dicts."""
        result = await self._call(
            self._dhan.intraday_daily_minute_charts,
            security_id      = security_id,
            exchange_segment = "NSE_EQ",
            instrument_type  = "EQUITY",
            interval         = interval,
            from_date        = from_date,
            to_date          = to_date,
        )
        return result.get("data", []) if isinstance(result, dict) else []

    # ── Market order (square off) ─────────────────────────────────────────────

    async def market_exit(
        self,
        security_id:      str,
        direction:        str,   # the original trade direction
        quantity:         int,
        product_type:     str,
    ) -> dict[str, Any]:
        """Immediately exit a position at market price."""
        exit_side = "SELL" if direction == "LONG" else "BUY"
        return await self.place_order(
            security_id      = security_id,
            transaction_type = exit_side,
            quantity         = quantity,
            order_type       = "MARKET",
            product_type     = product_type,
            tag              = "yukti-exit",
        )


# Module singleton — initialised lazily when first imported
dhan = DhanClient()
