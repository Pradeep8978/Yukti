"""
yukti/scheduler/calendar.py
NSE trading calendar — fetches holidays from NSE API with local cache and fallback.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

KOLKATA = ZoneInfo("Asia/Kolkata")

# Cache under project root data/ directory
_CACHE_FILE = Path(__file__).resolve().parents[2] / "data" / "nse_holidays_cache.json"
_CACHE_MAX_AGE_DAYS = 30

# Hardcoded fallback used when NSE API is unreachable and no cache exists
_FALLBACK: set[date] = {
    date(2025, 1, 26), date(2025, 3, 14), date(2025, 4, 14),
    date(2025, 4, 18), date(2025, 5, 1),  date(2025, 8, 15),
    date(2025, 10, 2), date(2025, 10, 24), date(2025, 11, 5),
    date(2025, 12, 25),
    date(2026, 1, 26), date(2026, 8, 15), date(2026, 10, 2),
    date(2026, 12, 25),
}

_NSE_BASE = "https://www.nseindia.com"
_HOLIDAY_API = f"{_NSE_BASE}/api/holiday-master?type=trading"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# In-process memory cache
_holidays: set[date] | None = None


def _load_cache() -> set[date] | None:
    """Load holidays from JSON cache if it exists and is not stale."""
    try:
        if not _CACHE_FILE.exists():
            return None
        raw = json.loads(_CACHE_FILE.read_text())
        fetched_at = datetime.fromisoformat(raw["fetched_at"])
        if datetime.now() - fetched_at > timedelta(days=_CACHE_MAX_AGE_DAYS):
            log.debug("NSE holiday cache is stale (>%d days old)", _CACHE_MAX_AGE_DAYS)
            return None
        return {date.fromisoformat(s) for s in raw["holidays"]}
    except Exception as exc:
        log.debug("NSE holiday cache unreadable: %s", exc)
        return None


def _save_cache(holidays: set[date]) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({
            "fetched_at": datetime.now().isoformat(),
            "holidays": sorted(d.isoformat() for d in holidays),
        }, indent=2))
        log.debug("NSE holiday cache saved: %d holidays", len(holidays))
    except Exception as exc:
        log.warning("Failed to save NSE holiday cache: %s", exc)


async def _fetch_from_nse() -> set[date]:
    """Fetch CM-segment trading holidays from the NSE holiday master API."""
    import httpx
    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        # Prime NSE session cookies — required for the API to respond
        await client.get(_NSE_BASE)
        resp = await client.get(_HOLIDAY_API)
        resp.raise_for_status()
        data = resp.json()

    holidays: set[date] = set()
    for entry in data.get("CM", []):          # CM = Capital Markets (equity/cash)
        raw = entry.get("tradingDate", "")
        try:
            holidays.add(datetime.strptime(raw, "%d-%b-%Y").date())
        except ValueError:
            continue
    return holidays


async def refresh_holidays() -> set[date]:
    """Fetch fresh holidays from NSE API, persist to cache, update memory.

    Falls back to the on-disk cache, then to hardcoded list if the API is down.
    Returns the resulting holiday set.
    """
    global _holidays
    try:
        fetched = await _fetch_from_nse()
        if fetched:
            _holidays = fetched
            _save_cache(fetched)
            log.info("NSE holidays refreshed from API: %d trading holidays", len(fetched))
            return fetched
        log.warning("NSE API returned empty holiday list; falling back")
    except Exception as exc:
        log.warning("NSE holiday fetch failed (%s); trying cache", exc)

    cached = _load_cache()
    if cached:
        _holidays = cached
        log.info("NSE holidays loaded from cache: %d holidays", len(cached))
        return cached

    _holidays = _FALLBACK.copy()
    log.warning("Using hardcoded NSE holiday fallback (%d holidays)", len(_holidays))
    return _holidays


def get_holidays() -> set[date]:
    """Return current in-memory holiday set (warm-loads from cache/fallback if needed)."""
    global _holidays
    if _holidays is None:
        cached = _load_cache()
        _holidays = cached if cached is not None else _FALLBACK.copy()
    return _holidays


def is_trading_day(d: date | None = None) -> bool:
    """True if `d` is a weekday (Mon–Fri) that is not an NSE holiday."""
    if d is None:
        d = datetime.now(KOLKATA).date()
    return d.weekday() < 5 and d not in get_holidays()


def is_trading_hours(now: datetime | None = None) -> bool:
    """True if the given time falls within NSE market hours (09:15–15:10 IST)."""
    if now is None:
        now = datetime.now(KOLKATA)
    elif now.tzinfo is None:
        now = datetime.fromtimestamp(now.timestamp(), tz=KOLKATA)
    else:
        now = now.astimezone(KOLKATA)
    t = now.time()
    return time(9, 15) <= t <= time(15, 10)
