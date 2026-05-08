"""
yukti/services/option_metrics_service.py
Fetches Nifty weekly option chain from DhanHQ and computes:
  - Put-Call Ratio (PCR) — OI-based market sentiment
  - Max Pain — strike where option buyers collectively lose the most
  - ATM Implied Volatility — expected daily range indicator

Cached in Redis for 30 minutes. Consumed by MacroContext → build_context() → Arjun.
Never raises — all failures return an empty OptionMetrics with NEUTRAL sentiment.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

_NIFTY_SECURITY_ID  = "13"
_NIFTY_EXCHANGE_SEG = "IDX_I"
_REDIS_KEY          = "yukti:market:option_metrics"
_TTL_SECONDS        = 1800  # 30 minutes


@dataclass
class OptionMetrics:
    pcr:       float | None = None   # Put-Call Ratio (OI-based)
    max_pain:  float | None = None   # Max pain strike level (₹)
    atm_iv:    float | None = None   # At-the-money implied volatility (%)
    sentiment: str = "NEUTRAL"       # BULLISH_OPTIONS | BEARISH_OPTIONS | NEUTRAL


def _nearest_expiry() -> str:
    """Return the nearest Thursday (weekly Nifty expiry) as YYYY-MM-DD."""
    today = date.today()
    days_ahead = (3 - today.weekday()) % 7   # Thursday = weekday 3; 0 if today is Thu
    return (today + timedelta(days=days_ahead)).isoformat()


def _extract_oc_list(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Navigate DhanHQ SDK response shapes to reach the per-strike list."""
    for accessor in [
        lambda d: d["data"]["oc_data"],
        lambda d: d["data"]["data"]["oc_data"],
        lambda d: d["oc_data"],
        lambda d: d["data"],
    ]:
        try:
            result = accessor(raw)
            if isinstance(result, list):
                return result
        except (KeyError, TypeError):
            continue
    return []


def compute_pcr(oc_list: list[dict]) -> float | None:
    """OI-based Put-Call Ratio. >1.3 = bearish fear, <0.7 = bullish greed."""
    total_call_oi = total_put_oi = 0
    for strike in oc_list:
        call = strike.get("call_options") or strike.get("CE") or {}
        put  = strike.get("put_options")  or strike.get("PE") or {}
        total_call_oi += int(call.get("open_interest") or call.get("oi") or 0)
        total_put_oi  += int(put.get("open_interest")  or put.get("oi")  or 0)
    if total_call_oi == 0:
        return None
    return round(total_put_oi / total_call_oi, 3)


def compute_max_pain(oc_list: list[dict]) -> float | None:
    """
    Strike that minimises total intrinsic value owed to option buyers.
    Acts as price gravity, especially on expiry day (Thursday).
    """
    strikes: list[float] = []
    for s in oc_list:
        try:
            k = float(s.get("strike_price") or s.get("strikePrice") or 0)
            if k > 0:
                strikes.append(k)
        except (TypeError, ValueError):
            continue
    if not strikes:
        return None

    min_pain = float("inf")
    max_pain_strike = None
    for candidate in strikes:
        pain = 0.0
        for s in oc_list:
            try:
                k        = float(s.get("strike_price") or s.get("strikePrice") or 0)
                call     = s.get("call_options") or s.get("CE") or {}
                put      = s.get("put_options")  or s.get("PE") or {}
                call_oi  = int(call.get("open_interest") or call.get("oi") or 0)
                put_oi   = int(put.get("open_interest")  or put.get("oi")  or 0)
                if k <= candidate:
                    pain += max(0.0, candidate - k) * call_oi
                if k >= candidate:
                    pain += max(0.0, k - candidate) * put_oi
            except (TypeError, ValueError):
                continue
        if pain < min_pain:
            min_pain = pain
            max_pain_strike = candidate
    return max_pain_strike


def compute_atm_iv(oc_list: list[dict], spot: float) -> float | None:
    """IV of the call at the strike nearest to spot price."""
    if spot <= 0 or not oc_list:
        return None
    best_dist = float("inf")
    best_iv   = None
    for s in oc_list:
        try:
            k    = float(s.get("strike_price") or s.get("strikePrice") or 0)
            dist = abs(k - spot)
            if dist < best_dist:
                call = s.get("call_options") or s.get("CE") or {}
                iv   = float(call.get("implied_volatility") or call.get("iv") or 0)
                if iv > 0:
                    best_dist = dist
                    best_iv   = iv
        except (TypeError, ValueError):
            continue
    return best_iv


def _pcr_to_sentiment(pcr: float | None) -> str:
    if pcr is None:
        return "NEUTRAL"
    if pcr > 1.3:
        return "BEARISH_OPTIONS"
    if pcr < 0.7:
        return "BULLISH_OPTIONS"
    return "NEUTRAL"


async def fetch_nifty_option_metrics() -> OptionMetrics:
    """
    Fetch Nifty weekly option chain, compute PCR / max-pain / ATM-IV.
    Redis-cached for 30 min. Never raises.
    """
    from yukti.data.state import get_redis
    from yukti.execution.broker_factory import get_broker

    r = await get_redis()

    cached = await r.get(_REDIS_KEY)
    if cached:
        try:
            d = json.loads(cached)
            return OptionMetrics(
                pcr=d.get("pcr"),
                max_pain=d.get("max_pain"),
                atm_iv=d.get("atm_iv"),
                sentiment=d.get("sentiment", "NEUTRAL"),
            )
        except Exception:
            pass

    try:
        broker  = get_broker()
        expiry  = _nearest_expiry()
        raw     = await broker.fetch_option_chain(_NIFTY_SECURITY_ID, _NIFTY_EXCHANGE_SEG, expiry)
        oc_list = _extract_oc_list(raw)
        if not oc_list:
            log.debug("option_metrics: empty option chain from DhanHQ")
            return OptionMetrics()

        spot = 0.0
        try:
            underlying = raw.get("data", {})
            spot = float(
                underlying.get("last_price")
                or underlying.get("underlying_spot_price")
                or underlying.get("underlyingSpotPrice")
                or 0
            )
        except Exception:
            pass

        pcr       = compute_pcr(oc_list)
        max_pain  = compute_max_pain(oc_list)
        atm_iv    = compute_atm_iv(oc_list, spot)
        sentiment = _pcr_to_sentiment(pcr)
        metrics   = OptionMetrics(pcr=pcr, max_pain=max_pain, atm_iv=atm_iv, sentiment=sentiment)

        try:
            await r.set(_REDIS_KEY, json.dumps({
                "pcr": pcr, "max_pain": max_pain, "atm_iv": atm_iv, "sentiment": sentiment,
            }), ex=_TTL_SECONDS)
        except Exception:
            pass

        log.info(
            "OptionMetrics: PCR=%.3f max_pain=%.0f atm_iv=%.1f%% sentiment=%s",
            pcr or 0, max_pain or 0, atm_iv or 0, sentiment,
        )
        return metrics

    except Exception as exc:
        log.debug("option_metrics fetch failed (non-fatal): %s", exc)
        return OptionMetrics()
