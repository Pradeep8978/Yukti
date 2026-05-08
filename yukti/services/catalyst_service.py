"""yukti/services/catalyst_service.py

Per-symbol catalyst feed.

A discretionary trader checks three things before the open:
  1. Did this name report results / get an order win / announce M&A?
  2. Is it inside its earnings window (±2 days)?
  3. Is there a pending board action (dividend, buyback, split)?

This service answers those questions at the symbol level, caches the
result in Redis (`yukti:catalysts:{symbol}`, TTL 24h), and exposes a
graded score (0–15) that the universe scanner can consume in place of
the old binary `has_catalyst` flag.

The scanner imports `score_catalyst_for_symbol()` and `is_pre_results()`
synchronously after `refresh()` has populated the cache.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from typing import Iterable

from yukti.config import settings
from yukti.services.news_provider import (
    NewsItem,
    classify_headline,
    get_news_provider,
)

log = logging.getLogger(__name__)


_CATALYST_PREFIX = "yukti:catalysts:"
_CALENDAR_KEY = "yukti:catalysts:calendar"
_TTL_SECONDS = 24 * 3600


# ── Sub-scores (sum capped at 15) ────────────────────────────────────────────
# Hard catalyst (M&A / order-win / fresh results): 8
# Pre-results window (±2 days from earnings):       4
# Soft catalyst (board / dividend / buyback):       3

_HARD_CATEGORIES = {"M&A", "GUIDANCE", "RESULTS"}
_SOFT_CATEGORIES = {"BOARD"}


def _score_items(items: Iterable[NewsItem]) -> int:
    pts = 0
    has_hard = any(i.category in _HARD_CATEGORIES for i in items)
    has_soft = any(i.category in _SOFT_CATEGORIES for i in items)
    if has_hard:
        pts += 8
    if has_soft:
        pts += 3
    return pts


def score_catalyst_for_symbol(
    symbol: str,
    items: Iterable[NewsItem] | None = None,
    in_results_window: bool = False,
) -> int:
    """Return 0..15 catalyst score for a symbol given its news items and
    whether we're inside its ±N-day earnings window."""
    base = _score_items(items or [])
    if in_results_window:
        base += 4
    return min(base, 15)


# ── Refresh: pull provider + earnings calendar, write per-symbol cache ──────

async def refresh(provider_name: str | None = None) -> dict[str, int]:
    """Fetch fresh announcements + earnings calendar, write Redis cache.

    Returns a small summary dict for logging:
        {"items": N, "symbols": M, "calendar_entries": K}
    """
    if not getattr(settings, "enable_news_enrichment", True):
        log.info("Catalyst refresh skipped: enable_news_enrichment=False")
        return {"items": 0, "symbols": 0, "calendar_entries": 0}

    name = provider_name or getattr(settings, "news_provider", "nse_only")
    api_key = getattr(settings, "news_provider_api_key", "")
    provider = get_news_provider(name, api_key=api_key)

    lookback = int(getattr(settings, "news_lookback_hours", 24))
    items = await provider.fetch_recent(lookback)
    log.info("CatalystService: provider=%s pulled %d items", provider.name, len(items))

    grouped: dict[str, list[NewsItem]] = {}
    for it in items:
        grouped.setdefault(it.symbol, []).append(it)

    # Earnings calendar — best-effort. NSE event-calendar requires the same
    # session-cookie dance; failures here are non-fatal.
    calendar = await _fetch_earnings_calendar()

    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        for sym, syms_items in grouped.items():
            await r.set(
                f"{_CATALYST_PREFIX}{sym}",
                json.dumps([asdict(i) for i in syms_items]),
                ex=_TTL_SECONDS,
            )
        if calendar:
            await r.set(_CALENDAR_KEY, json.dumps(calendar), ex=_TTL_SECONDS)
    except Exception as exc:
        log.warning("CatalystService: Redis write failed: %s", exc)

    return {
        "items": len(items),
        "symbols": len(grouped),
        "calendar_entries": len(calendar),
    }


async def _fetch_earnings_calendar() -> dict[str, str]:
    """Best-effort fetch of NSE event calendar (results dates).

    Returns `{symbol: "YYYY-MM-DD"}` for upcoming results.
    Empty dict on any error.
    """
    import httpx
    url = "https://www.nseindia.com/api/event-calendar"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            await client.get("https://www.nseindia.com", headers=headers)
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.debug("CatalystService: calendar fetch failed: %s", exc)
        return {}

    rows = data if isinstance(data, list) else data.get("data", [])
    out: dict[str, str] = {}
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        date_raw = row.get("date") or row.get("eventDate")
        if not sym or not date_raw:
            continue
        # Best-effort parse — keep raw string if parsing fails.
        try:
            d = datetime.strptime(date_raw, "%d-%b-%Y").date().isoformat()
        except Exception:
            d = date_raw
        out[sym] = d
    return out


# ── Sync helpers used by the scanner during scoring ─────────────────────────

async def get_items_for_symbol(symbol: str) -> list[NewsItem]:
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        raw = await r.get(f"{_CATALYST_PREFIX}{symbol}")
        if not raw:
            return []
        return [NewsItem(**row) for row in json.loads(raw)]
    except Exception as exc:
        log.debug("CatalystService: read failed for %s: %s", symbol, exc)
        return []


async def is_pre_results(symbol: str, window_days: int | None = None) -> bool:
    if window_days is None:
        window_days = int(getattr(settings, "results_window_days", 2))
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        raw = await r.get(_CALENDAR_KEY)
        if not raw:
            return False
        cal = json.loads(raw)
    except Exception:
        return False
    date_str = cal.get(symbol)
    if not date_str:
        return False
    try:
        d = datetime.fromisoformat(date_str).date()
    except Exception:
        return False
    today = datetime.now(timezone.utc).date()
    return abs((d - today).days) <= window_days
