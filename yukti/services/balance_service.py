"""
yukti/services/balance_service.py
Fetches live available trading balance from DhanHQ and caches in Redis.

Live/shadow mode : queries DhanHQ get_fund_limits() and caches for 2 min.
Paper/backtest   : returns settings.account_value (simulated capital).
Any API failure  : falls back to settings.account_value so trading continues.

Consumed by MarketScanService before each scan cycle to get the real
account_value for position sizing and exposure gate calculations.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_REDIS_KEY   = "yukti:account:available_balance"
_TTL_SECONDS = 120   # refresh every 2 minutes


def _parse_available_balance(raw: dict[str, Any]) -> float | None:
    """
    Extract the spendable balance from a DhanHQ fund_limits response.
    DhanHQ has a known typo ('availabelBalance') and varies across SDK versions,
    so we try every known key name.
    """
    if not isinstance(raw, dict):
        return None
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        return None
    for key in (
        "availabelBalance",       # DhanHQ typo — present in most SDK versions
        "availableBalance",
        "available_balance",
        "netAvailableMargin",
        "net_available_margin",
        "sodLimit",               # start-of-day limit as last resort
    ):
        val = data.get(key)
        if val is not None:
            try:
                f = float(val)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                continue
    return None


async def fetch_available_balance() -> float:
    """
    Return the usable account balance in ₹.

    In live/shadow mode this queries DhanHQ's fund limits API and caches
    the result in Redis for 2 minutes. In paper/backtest mode it returns
    settings.account_value directly (no API call needed for simulated capital).

    Always returns a positive float — falls back to settings.account_value
    if the API call fails or returns an unusable value.
    """
    from yukti.config import settings

    if settings.mode not in ("live", "shadow"):
        return settings.account_value

    from yukti.data.state import get_redis

    r = await get_redis()

    cached = await r.get(_REDIS_KEY)
    if cached:
        try:
            val = float(cached)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass

    try:
        from yukti.execution.broker_factory import get_broker
        raw = await get_broker().get_fund_limits()
        bal = _parse_available_balance(raw)
        if bal is not None:
            await r.set(_REDIS_KEY, str(bal), ex=_TTL_SECONDS)
            log.info("Live balance from DhanHQ: ₹%.2f (cached 2 min)", bal)
            return bal
        log.debug("fetch_available_balance: no usable balance in response: %s", raw)
    except Exception as exc:
        log.debug("fetch_available_balance failed (using config fallback): %s", exc)

    return settings.account_value
