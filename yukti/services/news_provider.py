"""yukti/services/news_provider.py

Pluggable news / corporate-announcement providers.

A successful trader's morning brief is a stack of catalysts: results, order
wins, M&A, board meetings, dividends. The `NewsProvider` interface lets us
plug different sources behind the catalyst service while keeping the rest of
the codebase ignorant of who fetched what.

Default impl is `NSEAnnouncementsProvider` (free, no API key). Optional
providers (Marketaux, NewsAPI) can be added later — gated by
`settings.news_provider_api_key`. If a paid provider is selected but no key
is configured, we degrade gracefully to NSE-only rather than erroring.

Every `NewsItem` returned has its `sentiment` field populated using the
finance-aware lexicon + VADER scorer in `yukti.signals.sentiment`. No AI,
no external API — pure rule-based NLP that runs in <1 ms per headline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Iterable, Literal, Protocol

import httpx

from yukti.signals.sentiment import score_headline

log = logging.getLogger(__name__)

Category = Literal["RESULTS", "M&A", "GUIDANCE", "BOARD", "OTHER"]


@dataclass
class NewsItem:
    symbol: str
    ts: str          # ISO-8601
    category: Category
    headline: str
    url: str = ""
    sentiment: float | None = None  # optional, in [-1, 1]


# ── Lightweight category classifier ──────────────────────────────────────────
# Trader rule of thumb: do title-keyword routing first; LLMs are too expensive
# to call on every announcement.

_CATEGORY_KEYWORDS: dict[Category, tuple[str, ...]] = {
    "RESULTS":  ("results", "quarterly", "q1", "q2", "q3", "q4", "earnings",
                 "revenue", "pat", "ebitda", "profit"),
    "M&A":      ("acquisition", "acquires", "acquir", "merger", "amalgamation",
                 "stake", "open offer", "demerger", "spin-off", "divest"),
    "GUIDANCE": ("order", "contract", "capex", "expansion", "guidance",
                 "fund-rais", "fundrais", "qip", "preferential issue",
                 "warrants"),
    "BOARD":    ("board meeting", "dividend", "buyback", "stock split",
                 "bonus", "record date", "agm", "egm"),
}


def classify_headline(headline: str) -> Category:
    h = (headline or "").lower()
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(k in h for k in kws):
            return cat
    return "OTHER"


def enrich_item(item: NewsItem) -> NewsItem:
    """Fill `item.sentiment` in-place using the rule-based scorer. Returns item."""
    if item.sentiment is None:
        item.sentiment = score_headline(item.headline)
    return item


# ── Provider interface ──────────────────────────────────────────────────────

class NewsProvider(Protocol):
    name: str

    async def fetch_recent(self, lookback_hours: int) -> list[NewsItem]: ...


# ── NSE corporate announcements (default, free) ─────────────────────────────

_NSE_ANNOUNCEMENTS_URL = (
    "https://www.nseindia.com/api/corporate-announcements?index=equities"
)
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
}


class NSEAnnouncementsProvider:
    """Pulls today's corporate announcements from NSE's public endpoint.

    NSE requires a session cookie — we prime it by hitting the homepage
    first, exactly like `scripts/universe_loader.py` does.
    """

    name = "nse"

    async def fetch_recent(self, lookback_hours: int = 24) -> list[NewsItem]:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
                # Prime cookies
                await client.get("https://www.nseindia.com", headers=_NSE_HEADERS)
                resp = await client.get(_NSE_ANNOUNCEMENTS_URL, headers=_NSE_HEADERS)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            log.warning("NSE announcements fetch failed: %s", exc)
            return []

        rows = payload if isinstance(payload, list) else payload.get("data", [])
        cutoff = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
        items: list[NewsItem] = []
        for row in rows:
            sym = (row.get("symbol") or row.get("sym") or "").strip().upper()
            headline = (row.get("subject") or row.get("desc") or "").strip()
            ts_raw = row.get("an_dt") or row.get("dt") or row.get("date")
            if not sym or not headline:
                continue
            try:
                ts = _parse_nse_ts(ts_raw)
            except Exception:
                ts = datetime.now(timezone.utc)
            if ts.timestamp() < cutoff:
                continue
            items.append(enrich_item(NewsItem(
                symbol=sym,
                ts=ts.isoformat(),
                category=classify_headline(headline),
                headline=headline,
                url=row.get("attchmntFile") or "",
            )))
        return items


def _parse_nse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    # Common NSE formats: "13-Apr-2026 14:32:01" or "2026-04-13 14:32:01"
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return datetime.now(timezone.utc)


# ── Stub for paid providers — degrades to NSE if no key ─────────────────────

class MarketauxNewsProvider:
    """Marketaux integration stub — gated on `news_provider_api_key`.
    Implementation deliberately minimal; falls back to no-op if no key."""

    name = "marketaux"

    def __init__(self, api_key: str = "") -> None:
        self._key = api_key

    async def fetch_recent(self, lookback_hours: int = 24) -> list[NewsItem]:
        if not self._key:
            log.info("Marketaux provider disabled: news_provider_api_key not set")
            return []
        log.warning("Marketaux provider not yet implemented — returning empty list")
        return []


# ── Factory ────────────────────────────────────────────────────────────────

def get_news_provider(name: str | None, api_key: str = "") -> NewsProvider:
    n = (name or "nse_only").lower()
    if n in ("nse", "nse_only"):
        return NSEAnnouncementsProvider()
    if n == "marketaux":
        return MarketauxNewsProvider(api_key=api_key)
    log.warning("Unknown news_provider %r — defaulting to NSE", name)
    return NSEAnnouncementsProvider()


def items_to_json(items: Iterable[NewsItem]) -> list[dict]:
    return [asdict(i) for i in items]
