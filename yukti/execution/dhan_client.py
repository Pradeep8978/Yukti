"""
yukti/execution/dhan_client.py
Thin async wrapper around the dhanhq SDK.
Handles retries, rate limiting, and maps to DhanHQ constants.
"""
from __future__ import annotations

import asyncio
import os
import httpx
from datetime import date
import logging
import time
import uuid
from functools import wraps
from typing import Any, Callable

from dhanhq import dhanhq, DhanContext
import yfinance as yf
import pandas as pd
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
        # Resolve environment (sandbox overrides production when enabled)
        cid = settings.dhan_client_id
        base = settings.dhan_base_url
        use_sandbox = settings.dhan_use_sandbox

        if use_sandbox:
            cid = settings.dhan_sandbox_client_id or cid
            base = settings.dhan_sandbox_base_url
            log.info("DhanClient: Using SANDBOX environment")

        # Gather credentials (use sandbox access token when sandbox enabled)
        access_token = settings.dhan_sandbox_access_token if use_sandbox else settings.dhan_access_token

        # Prefer using an access token (only supported auth method in current flow)
        ctx = None
        self._cid = cid
        self._base = base.rstrip('/')
        self._access_token = access_token
        self._auth_method = None

        if access_token:
            try:
                ctx = DhanContext(client_id=cid, access_token=access_token)
                self._auth_method = 'access_token'
                log.info("DhanClient: using access_token auth")
            except TypeError:
                log.debug("DhanContext(access_token) unsupported; attempting context without token")

        if ctx is None:
            # Try to create a context without an access token; calls will surface auth errors.
            try:
                ctx = DhanContext(client_id=cid)
                self._auth_method = 'none'
                log.warning("DhanClient: no access token configured; API calls may fail")
            except Exception:
                log.exception("DhanClient: failed to initialize DhanContext without token")

        if hasattr(ctx, 'dhan_http') and base:
            ctx.dhan_http.base_url = base.rstrip('/')
            if "sandbox" in ctx.dhan_http.base_url and not ctx.dhan_http.base_url.endswith("/v2"):
                ctx.dhan_http.base_url += "/v2"

        self._dhan = dhanhq(ctx)
        self._loop = asyncio.get_event_loop()

    async def _fetch_renewed_token(self) -> str | None:
        """Native-async call to the Dhan RenewToken endpoint via httpx.

        Returns the new access token string on success, otherwise None.
        """
        if not self._access_token:
            log.debug("DhanClient: no access token present to renew")
            return None
        url = self._base + '/RenewToken'
        headers = {
            'access-token': str(self._access_token),
            'dhanClientId': str(self._cid),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=headers)
        except Exception as exc:
            log.exception("DhanClient: RenewToken request failed: %s", exc)
            return None

        try:
            data = r.json()
        except Exception:
            log.warning("DhanClient: RenewToken non-json response: %s", r.text[:200])
            return None
        if r.status_code != 200:
            log.warning("DhanClient: RenewToken failed status=%s body=%s", r.status_code, r.text[:400])
            return None
        new_token = data.get('accessToken') or data.get('access_token') or data.get('AccessToken')
        if new_token:
            return new_token
        if isinstance(data.get('data'), dict):
            return data['data'].get('accessToken') or data['data'].get('access_token')
        return None

    async def _renew_access_token(self) -> bool:
        """Renew the access token and reinitialise the client if successful."""
        new_token = await self._fetch_renewed_token()
        if not new_token:
            return False
        # Update runtime settings and recreate underlying client
        try:
            import os as _os
            _os.environ['DHAN_ACCESS_TOKEN'] = new_token
        except Exception:
            pass
        try:
            settings.dhan_access_token = new_token
        except Exception:
            pass
        self._access_token = new_token
        # Recreate context + client using new token
        try:
            ctx = DhanContext(client_id=self._cid, access_token=new_token)
            if hasattr(ctx, 'dhan_http') and self._base:
                ctx.dhan_http.base_url = self._base
            self._dhan = dhanhq(ctx)
            log.info("DhanClient: access token renewed and client reinitialized")
            return True
        except Exception as exc:
            log.exception("DhanClient: failed to reinitialize client after token renewal: %s", exc)
            return False

    async def _call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous SDK call in the thread pool + rate limiter.

        On authentication failures, attempt to renew the access token once and
        retry the call.
        """
        await _bucket.acquire()
        try:
            return await self._loop.run_in_executor(None, lambda: fn(*args, **kwargs))
        except Exception as exc:
            msg = str(exc) or ""
            # Detect likely auth errors from Dhan (HTTP 401 / DH-901 / token issues)
            if any(token in msg for token in ("401", "DH-901", "access token is invalid", "invalid or expired")) or "Unauthorized" in msg:
                log.info("DhanClient: detected authentication error, attempting token renew: %s", msg[:200])
                try:
                    renewed = await self._renew_access_token()
                except Exception:
                    renewed = False
                if renewed:
                    # Retry once after successful renewal
                    try:
                        return await self._loop.run_in_executor(None, lambda: fn(*args, **kwargs))
                    except Exception as exc2:
                        log.warning("DhanClient: retry after token renewal failed: %s", exc2)
                        raise
            # Not an auth failure or renew failed — re-raise original exception
            raise

    # ── Paper-mode safety guard ────────────────────────────────────────────────

    def _assert_not_paper(self, operation: str) -> None:
        """Raise if we're in paper mode — prevents accidental real orders."""
        if settings.mode == "paper":
            raise RuntimeError(
                f"DhanClient.{operation}() blocked: agent is in PAPER mode. "
                f"Real orders are disabled. Use PaperBrokerWrapper instead."
            )

    # ── Orders ────────────────────────────────────────────────────────────────

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
        idempotency_key:  str | None = None,
    ) -> dict[str, Any]:
        """Place an order with lightweight idempotency checks across retries.

        Strategy:
        - Generate a stable `idempotency_key` for this high-level call if not provided.
        - Include it in the `tag` field when calling DhanHQ.
        - On transient failures, query recent orders for a matching tag and return it if found.
        """
        self._assert_not_paper("place_order")

        if idempotency_key is None:
            idempotency_key = uuid.uuid4().hex

        tag_value = f"{tag}|id={idempotency_key}" if tag else f"id={idempotency_key}"

        attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
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
                    tag              = tag_value,
                )
                log.info("place_order %s %s qty=%d → %s", transaction_type, security_id, quantity, result)
                return result
            except Exception as exc:
                last_exc = exc
                log.warning("place_order attempt %d failed: %s", attempt, exc)
                # Check for previously placed order that matches our idempotency key
                try:
                    orders = await self.get_order_list()
                    for o in orders:
                        o_tag = None
                        if isinstance(o, dict):
                            o_tag = o.get("tag") or (o.get("data") and o.get("data").get("tag"))
                        if o_tag and f"id={idempotency_key}" in str(o_tag):
                            log.info("Found existing order for idempotency key %s: %s", idempotency_key, o)
                            return o
                except Exception:
                    log.debug("Idempotency check via get_order_list() failed; will retry")

                if attempt < attempts:
                    await asyncio.sleep(min(5, 2 ** (attempt - 1)))

        # All attempts exhausted — surface last exception
        if last_exc:
            raise last_exc
        raise RuntimeError("place_order failed without exception")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5), reraise=True)
    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._assert_not_paper("cancel_order")
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
        self._assert_not_paper("place_gtt")
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
        self._assert_not_paper("cancel_gtt")
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
        symbol:       str = "",
    ) -> list[dict[str, Any]]:
        """Fetch historical candles. Falls back to yfinance if Dhan fails or is unavailable."""

        def _fetch_from_dhan() -> list[dict[str, Any]]:
            """Run DhanHQ SDK call and convert dict-of-arrays to list-of-dicts."""
            res = self._dhan.intraday_minute_data(
                security_id      = security_id,
                exchange_segment = "NSE_EQ" if security_id != "13" else "IDX_I",
                instrument_type  = "EQUITY" if security_id != "13" else "INDEX",
                interval         = interval,
                from_date        = from_date,
                to_date          = to_date,
            )
            raw = res.get("data", []) if isinstance(res, dict) else []
            if isinstance(raw, dict):
                from datetime import datetime as _dt
                ts_list = raw.get("timestamp", [])
                return [
                    {
                        "time":   _dt.utcfromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S"),
                        "open":   float(raw["open"][i]),
                        "high":   float(raw["high"][i]),
                        "low":    float(raw["low"][i]),
                        "close":  float(raw["close"][i]),
                        "volume": float(raw["volume"][i]),
                    }
                    for i, ts in enumerate(ts_list)
                ]
            return raw if isinstance(raw, list) else []

        # Two attempts — DhanHQ SDK is synchronous and not thread-safe under
        # concurrent run_in_executor calls; a single retry covers transient races.
        data: list[dict[str, Any]] = []
        for attempt in range(2):
            try:
                await _bucket.acquire()
                rows = await self._loop.run_in_executor(None, _fetch_from_dhan)
                if rows:
                    data = rows
                    log.debug("DhanClient: %d candles from DhanHQ for %s (attempt %d)", len(data), security_id, attempt + 1)
                    break
                if attempt == 0:
                    log.debug("DhanClient: empty DhanHQ response for %s — retrying once", security_id)
                    await asyncio.sleep(0.5)
            except Exception as exc:
                log.debug("DhanClient: DhanHQ candle fetch failed for %s (attempt %d): %s", security_id, attempt + 1, exc)
                if attempt == 0:
                    await asyncio.sleep(0.5)

        # ── Fallback to yfinance ──────────────────────────────────────────
        if not data and symbol:
            log.info("DhanClient: No data for %s, trying yfinance fallback...", symbol)
            try:
                s_int = str(interval)
                yf_interval = "5m"
                if s_int.isdigit():
                    yf_interval = f"{s_int}m"
                    if s_int == "1" and from_date and to_date:
                        try:
                            from_day = date.fromisoformat(str(from_date))
                            to_day = date.fromisoformat(str(to_date))
                            if (to_day - from_day).days > 8:
                                yf_interval = "1d"
                        except ValueError:
                            log.debug("DhanClient: could not parse candle date range %s -> %s", from_date, to_date)
                ticker_sym = f"{symbol}.NS" if security_id != "13" else "^NSEI"
                
                # yfinance uses start/end dates
                # Optimization: yfinance is much faster for recent data
                ticker = yf.Ticker(ticker_sym)
                df = ticker.history(start=from_date, end=to_date, interval=yf_interval)
                
                if not df.empty:
                    # Convert to Dhan format: list of dicts with 'time', 'open', 'high', 'low', 'close', 'volume'
                    data = []
                    for ts, row in df.iterrows():
                        data.append({
                            "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                            "open": float(row["Open"]),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"]),
                            "volume": int(row["Volume"]),
                        })
                    log.info("DhanClient: Successfully fetched %d candles from yfinance for %s", len(data), symbol)
            except Exception as e:
                log.warning("DhanClient: yfinance fallback failed for %s: %s", symbol, e)

        return data

    async def quote_snapshot(
        self,
        security_ids: list[str],
        chunk_size: int = 100,
    ) -> dict[str, dict[str, Any]]:
        """Bulk LTP / OHLC / volume snapshot for many securities.

        Returns a dict keyed by security_id. Uses the dhanhq SDK's quote_data
        endpoint when available; chunks the request to stay within DhanHQ's
        per-call limit. Falls back to per-symbol intraday candles if quote_data
        is unavailable on the installed SDK version.
        """
        if not security_ids:
            return {}

        out: dict[str, dict[str, Any]] = {}
        quote_fn = getattr(self._dhan, "quote_data", None) or getattr(self._dhan, "ohlc_data", None)
        for i in range(0, len(security_ids), chunk_size):
            batch = security_ids[i : i + chunk_size]
            if quote_fn is not None:
                try:
                    payload = {"NSE_EQ": [int(s) for s in batch if str(s).isdigit()]}
                    res = await self._call(quote_fn, securities=payload)
                    data = res.get("data", {}) if isinstance(res, dict) else {}
                    nse_block = data.get("data", {}).get("NSE_EQ") if isinstance(data, dict) else None
                    if nse_block is None and isinstance(data, dict):
                        nse_block = data.get("NSE_EQ")
                    if isinstance(nse_block, dict):
                        for sid, q in nse_block.items():
                            out[str(sid)] = q if isinstance(q, dict) else {"raw": q}
                    continue
                except Exception as exc:
                    log.warning("quote_snapshot batch failed (%d ids): %s — falling back", len(batch), exc)

            for sid in batch:
                try:
                    today = date.today().isoformat()
                    raw = await self.get_candles(sid, "1", today, today)
                    if raw:
                        last = raw[-1]
                        out[str(sid)] = {
                            "last_price": float(last.get("close", 0)),
                            "open":       float(last.get("open", 0)),
                            "high":       float(last.get("high", 0)),
                            "low":        float(last.get("low", 0)),
                            "volume":     int(last.get("volume", 0)),
                        }
                except Exception as exc:
                    log.debug("quote_snapshot fallback failed for %s: %s", sid, exc)
        return out

    async def historical_daily(
        self,
        security_id: str,
        days: int = 60,
        symbol: str = "",
    ) -> list[dict[str, Any]]:
        """Daily OHLCV for the last `days` sessions. Replaces yfinance for
        volatility / RS calculations when DhanHQ's daily endpoint is available.
        """
        from datetime import datetime, timedelta

        to_d = datetime.now().date()
        from_d = to_d - timedelta(days=int(days * 1.6) + 5)  # cushion for weekends/holidays

        daily_fn = getattr(self._dhan, "historical_daily_data", None)
        if daily_fn is not None:
            try:
                res = await self._call(
                    daily_fn,
                    security_id      = security_id,
                    exchange_segment = "NSE_EQ",
                    instrument_type  = "EQUITY",
                    from_date        = from_d.isoformat(),
                    to_date          = to_d.isoformat(),
                )
                rows = res.get("data", []) if isinstance(res, dict) else []
                # DhanHQ may return dict-of-arrays — same pattern as intraday
                if isinstance(rows, dict):
                    try:
                        ts_list = rows.get("timestamp", [])
                        rows = [
                            {
                                "time":   str(ts)[:10],  # YYYY-MM-DD
                                "open":   float(rows["open"][i]),
                                "high":   float(rows["high"][i]),
                                "low":    float(rows["low"][i]),
                                "close":  float(rows["close"][i]),
                                "volume": float(rows["volume"][i]),
                            }
                            for i, ts in enumerate(ts_list)
                        ]
                        log.debug("historical_daily: converted dict-of-arrays to %d rows for %s", len(rows), security_id)
                    except Exception as _conv_exc:
                        log.warning("historical_daily: dict-of-arrays conversion failed: %s", _conv_exc)
                        rows = []
                if rows and isinstance(rows, list):
                    return rows[-days:]
            except Exception as exc:
                log.warning("historical_daily DhanHQ call failed for %s: %s — trying yfinance", security_id, exc)

        # Fallback: yfinance daily candles (always available, reliable pre-market)
        if symbol:
            try:
                ticker_sym = f"{symbol}.NS"
                ticker = yf.Ticker(ticker_sym)
                df = ticker.history(
                    start=from_d.isoformat(), end=to_d.isoformat(), interval="1d"
                )
                if not df.empty:
                    yf_rows = [
                        {
                            "time":   ts.strftime("%Y-%m-%d"),
                            "open":   float(row["Open"]),
                            "high":   float(row["High"]),
                            "low":    float(row["Low"]),
                            "close":  float(row["Close"]),
                            "volume": float(row["Volume"]),
                        }
                        for ts, row in df.iterrows()
                    ]
                    log.debug("historical_daily: yfinance returned %d daily rows for %s", len(yf_rows), symbol)
                    return yf_rows[-days:]
            except Exception as yf_exc:
                log.warning("historical_daily: yfinance fallback failed for %s: %s", symbol, yf_exc)

        return []

    # ── Option chain ──────────────────────────────────────────────────────────

    async def fetch_option_chain(
        self,
        under_security_id: str,
        under_exchange_segment: str,
        expiry: str,
    ) -> dict[str, Any]:
        """Fetch full option chain for an underlying. Returns raw SDK response."""
        fn = getattr(self._dhan, "option_chain", None)
        if fn is None:
            log.warning("DhanClient: option_chain not available in installed SDK")
            return {}
        return await self._call(fn, under_security_id, under_exchange_segment, expiry)

    async def get_market_depth(self, security_id: str) -> dict[str, Any]:
        """
        Returns top-of-book bid/ask spread for a single equity security.
        Returns {"spread_pct": float, "best_bid_qty": int, "best_ask_qty": int}
        or empty dict on failure / unsupported SDK.
        """
        try:
            quote_fn = (
                getattr(self._dhan, "market_quote", None)
                or getattr(self._dhan, "quote_data", None)
            )
            if quote_fn is None:
                return {}
            payload = {"NSE_EQ": [int(security_id)]}
            res = await self._call(quote_fn, securities=payload)
            data = res.get("data", {}) if isinstance(res, dict) else {}
            nse = data.get("data", {}).get("NSE_EQ") or data.get("NSE_EQ") or {}
            entry = nse.get(str(security_id)) or nse.get(int(security_id)) or {}
            if not entry:
                return {}
            best_bid     = float(entry.get("buy_price")      or entry.get("best_bid_price") or 0)
            best_ask     = float(entry.get("sell_price")      or entry.get("best_ask_price") or entry.get("offer_price") or 0)
            best_bid_qty = int(entry.get("buy_quantity")  or entry.get("best_bid_qty")   or 999)
            best_ask_qty = int(entry.get("sell_quantity") or entry.get("best_ask_qty")   or 999)
            mid          = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
            spread_pct   = ((best_ask - best_bid) / mid * 100) if mid > 0 else 0.0
            return {
                "spread_pct":    round(spread_pct, 4),
                "best_bid_qty":  best_bid_qty,
                "best_ask_qty":  best_ask_qty,
            }
        except Exception as exc:
            log.debug("get_market_depth failed for %s: %s", security_id, exc)
            return {}

    # ── Account funds ────────────────────────────────────────────────────────

    async def get_fund_limits(self) -> dict[str, Any]:
        """
        Fetch account fund limits from DhanHQ (available balance, used margin, etc).
        Returns raw SDK response. Never raises — returns {} on any failure.
        """
        try:
            fn = getattr(self._dhan, "get_fund_limits", None)
            if fn is None:
                log.debug("DhanClient: get_fund_limits not available in installed SDK")
                return {}
            return await self._call(fn)
        except Exception as exc:
            log.debug("get_fund_limits failed: %s", exc)
            return {}

    # ── Market order (square off) ─────────────────────────────────────────────

    async def market_exit(
        self,
        security_id:      str,
        direction:        str,   # the original trade direction
        quantity:         int,
        product_type:     str,
    ) -> dict[str, Any]:
        """Immediately exit a position at market price."""
        self._assert_not_paper("market_exit")
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
