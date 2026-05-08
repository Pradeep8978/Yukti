"""
yukti/services/macro_context_service.py

Fetches and caches macro market context for each scan cycle:
  - Nifty50 change % and trend direction (from DhanHQ candles)
  - India VIX level (from Yahoo Finance JSON)
  - FII / DII net flows today (from NSE public API)
  - Top 3 India market headlines (from Economic Times Markets RSS)

All data is cached in Redis so a cold restart doesn't re-hit every source.
Every fetch is individually try/except — a failed source degrades gracefully
and never blocks a scan cycle.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import feedparser
import httpx

log = logging.getLogger(__name__)

# ── Redis key constants ───────────────────────────────────────────────────────
_KEY_NIFTY_CHG      = "yukti:market:nifty_chg_pct"
_KEY_NIFTY_TREND    = "yukti:market:nifty_trend"
_KEY_VIX            = "yukti:market:india_vix"
_KEY_FII_NET        = "yukti:market:fii_net_cr"
_KEY_DII_NET        = "yukti:market:dii_net_cr"
_KEY_HEADLINES      = "yukti:market:headlines"

_TTL_NIFTY     = 600   # 10 min — Nifty + VIX
_TTL_FII       = 3600  # 1 hour — FII/DII updates a few times per day
_TTL_HEADLINES = 1800  # 30 min — RSS

_ET_MARKETS_RSS = "https://economictimes.indiatimes.com/markets/rss.cms"
_YAHOO_VIX_URL  = "https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX?interval=1d&range=1d"
_NSE_FII_URL    = "https://www.nseindia.com/api/fiidiiTradeReact"


@dataclass
class MacroContext:
    """Snapshot of macro conditions passed to build_context() each cycle."""
    nifty_chg_pct:     float = 0.0
    nifty_trend:       str   = "SIDEWAYS"
    india_vix:         Optional[float] = None
    fii_net_cr:        Optional[float] = None   # crores; negative = net selling
    dii_net_cr:        Optional[float] = None
    headlines:         list[str] = field(default_factory=list)
    # Option chain sentiment (Nifty weekly)
    nifty_pcr:         Optional[float] = None   # Put-Call Ratio
    nifty_max_pain:    Optional[float] = None   # Max pain strike (₹)
    nifty_atm_iv:      Optional[float] = None   # ATM implied volatility (%)
    options_sentiment: str = "NEUTRAL"          # BULLISH_OPTIONS | BEARISH_OPTIONS | NEUTRAL

    # ── Derived labels used in context prompt ─────────────────────────────────

    @property
    def vix_label(self) -> str:
        if self.india_vix is None:
            return "N/A"
        if self.india_vix < 15:
            return f"{self.india_vix:.1f} (low — calm, normal sizing)"
        if self.india_vix < 20:
            return f"{self.india_vix:.1f} (moderate)"
        if self.india_vix < 25:
            return f"{self.india_vix:.1f} (elevated — reduce size)"
        return f"{self.india_vix:.1f} (HIGH — consider skipping)"

    @property
    def fii_label(self) -> str:
        if self.fii_net_cr is None:
            return "N/A"
        sign = "+" if self.fii_net_cr >= 0 else ""
        direction = "buying" if self.fii_net_cr >= 0 else "selling"
        return f"{sign}₹{self.fii_net_cr:,.0f} Cr ({direction})"

    @property
    def dii_label(self) -> str:
        if self.dii_net_cr is None:
            return "N/A"
        sign = "+" if self.dii_net_cr >= 0 else ""
        direction = "buying — absorbing" if self.dii_net_cr >= 0 else "selling"
        return f"{sign}₹{self.dii_net_cr:,.0f} Cr ({direction})"

    @property
    def pcr_label(self) -> str:
        if self.nifty_pcr is None:
            return "N/A"
        if self.nifty_pcr > 1.3:
            return f"{self.nifty_pcr:.2f} (heavy put buying — bearish hedge)"
        if self.nifty_pcr < 0.7:
            return f"{self.nifty_pcr:.2f} (heavy call buying — bullish greed)"
        return f"{self.nifty_pcr:.2f} (neutral)"

    @property
    def headlines_text(self) -> str:
        if not self.headlines:
            return "  None available"
        return "\n".join(f"    • {h}" for h in self.headlines[:3])


# ── Sector keyword map ────────────────────────────────────────────────────────
# Maps NSE symbol prefixes / exact names to sector search terms.
# Checked case-insensitively against headline text.
_SECTOR_KEYWORDS: dict[str, list[str]] = {
    # Banking & Finance
    "HDFCBANK":   ["HDFC Bank", "HDFC", "banking", "RBI", "interest rate", "NPA"],
    "ICICIBANK":  ["ICICI Bank", "ICICI", "banking", "RBI", "interest rate", "NPA"],
    "SBIN":       ["SBI", "State Bank", "banking", "RBI", "PSU bank"],
    "AXISBANK":   ["Axis Bank", "banking", "RBI"],
    "KOTAKBANK":  ["Kotak", "banking", "RBI"],
    "BAJFINANCE": ["Bajaj Finance", "NBFC", "consumer finance"],
    # IT
    "TCS":        ["TCS", "Tata Consultancy", "IT sector", "tech", "software"],
    "INFY":       ["Infosys", "INFY", "IT sector", "tech", "software"],
    "WIPRO":      ["Wipro", "IT sector", "tech"],
    "HCLTECH":    ["HCL Tech", "HCL", "IT sector", "tech"],
    "TECHM":      ["Tech Mahindra", "IT sector", "telecom tech"],
    # Oil & Energy
    "RELIANCE":   ["Reliance", "RIL", "petrochemical", "oil", "Jio", "energy"],
    "ONGC":       ["ONGC", "oil", "crude", "energy", "PSU"],
    "BPCL":       ["BPCL", "Bharat Petroleum", "oil", "crude", "refinery"],
    "IOC":        ["Indian Oil", "IOC", "oil", "refinery", "crude"],
    # Auto
    "TATAMOTORS": ["Tata Motors", "auto", "EV", "JLR", "automobile"],
    "MARUTI":     ["Maruti", "Suzuki", "auto", "automobile", "passenger vehicle"],
    "M&M":        ["Mahindra", "M&M", "auto", "EV", "tractor", "SUV"],
    "BAJAJ-AUTO": ["Bajaj Auto", "two-wheeler", "motorcycle", "auto"],
    "HEROMOTOCO": ["Hero Moto", "two-wheeler", "motorcycle", "auto"],
    # Metals & Mining
    "TATASTEEL":  ["Tata Steel", "steel", "metal", "iron ore"],
    "JSWSTEEL":   ["JSW Steel", "steel", "metal"],
    "HINDALCO":   ["Hindalco", "aluminium", "metal", "Novelis"],
    "VEDL":       ["Vedanta", "zinc", "copper", "metal", "mining"],
    # Pharma
    "SUNPHARMA":  ["Sun Pharma", "pharma", "pharmaceutical", "drug"],
    "DRREDDY":    ["Dr Reddy", "pharma", "pharmaceutical", "generic"],
    "CIPLA":      ["Cipla", "pharma", "pharmaceutical", "drug"],
    # FMCG
    "HINDUNILVR": ["HUL", "Hindustan Unilever", "FMCG", "consumer goods"],
    "ITC":        ["ITC", "FMCG", "cigarette", "tobacco", "hotel"],
    "NESTLEIND":  ["Nestle", "FMCG", "food"],
    # Infra / Cement
    "ULTRACEMCO": ["UltraTech", "cement", "infrastructure"],
    "SHREECEM":   ["Shree Cement", "cement"],
    "ADANIPORTS": ["Adani Ports", "Adani", "port", "logistics"],
    "ADANIENT":   ["Adani", "energy", "power"],
    "POWERGRID":  ["Power Grid", "power", "electricity", "transmission"],
    "NTPC":       ["NTPC", "power", "electricity", "PSU"],
}


def filter_headlines_for_symbol(symbol: str, headlines: list[str]) -> list[str]:
    """
    Return headlines relevant to a specific NSE symbol.

    Matches on:
      1. The symbol string itself (e.g. "RELIANCE" in headline)
      2. Sector keywords from _SECTOR_KEYWORDS map

    Returns at most 3 matches. Returns [] if nothing relevant found.
    """
    if not headlines:
        return []

    symbol_upper = symbol.upper()
    keywords = _SECTOR_KEYWORDS.get(symbol_upper, [])
    # Always include the bare symbol name as a keyword
    search_terms = [symbol_upper] + keywords

    matches: list[str] = []
    for headline in headlines:
        hl_lower = headline.lower()
        if any(term.lower() in hl_lower for term in search_terms):
            matches.append(headline)
        if len(matches) == 3:
            break

    return matches


# ── Individual fetch helpers ──────────────────────────────────────────────────

async def _fetch_india_vix(client: httpx.AsyncClient) -> Optional[float]:
    """Fetch India VIX close from Yahoo Finance. Returns None on any error."""
    try:
        resp = await client.get(_YAHOO_VIX_URL, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return float(price)
    except Exception as exc:
        log.debug("VIX fetch failed: %s", exc)
        return None


async def _fetch_fii_dii(client: httpx.AsyncClient) -> tuple[Optional[float], Optional[float]]:
    """
    Fetch FII and DII net flows from NSE public API.
    NSE requires browser-like headers and a session cookie — best-effort only.
    Returns (fii_net, dii_net) in crores, or (None, None) on failure.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        # NSE needs a session cookie — prime it with a homepage request first
        await client.get("https://www.nseindia.com", headers=headers, timeout=5.0)
        resp = await client.get(_NSE_FII_URL, headers=headers, timeout=8.0)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None, None

        fii_net = dii_net = None
        for row in rows:
            category = str(row.get("category", "")).upper()
            net = row.get("netPurchasesSales")
            if net is not None:
                if "FII" in category or "FPI" in category:
                    fii_net = float(net)
                elif "DII" in category:
                    dii_net = float(net)
        return fii_net, dii_net
    except Exception as exc:
        log.debug("FII/DII fetch failed: %s", exc)
        return None, None


async def _fetch_headlines() -> list[str]:
    """Parse ET Markets RSS feed for top 3 headlines. Returns [] on failure."""
    try:
        loop = asyncio.get_event_loop()
        feed = await loop.run_in_executor(None, feedparser.parse, _ET_MARKETS_RSS)
        titles: list[str] = []
        for entry in feed.entries[:3]:
            title = getattr(entry, "title", "").strip()
            if title:
                titles.append(title)
        return titles
    except Exception as exc:
        log.debug("Headlines fetch failed: %s", exc)
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

async def fetch_macro_context(nifty_chg: float, nifty_trend: str) -> MacroContext:
    """
    Assemble a MacroContext for the current scan cycle.

    Nifty data is passed in (already fetched + cached by _get_nifty_context).
    VIX, FII/DII, and headlines are fetched here in parallel, each with
    Redis caching so repeated calls within TTL are instant.

    Never raises — all failures produce None/empty fields.
    """
    from yukti.data.state import get_redis

    ctx = MacroContext(nifty_chg_pct=nifty_chg, nifty_trend=nifty_trend)
    r = await get_redis()

    # ── Check cache first ─────────────────────────────────────────────────────
    cached_vix      = await r.get(_KEY_VIX)
    cached_fii      = await r.get(_KEY_FII_NET)
    cached_dii      = await r.get(_KEY_DII_NET)
    cached_headlines = await r.get(_KEY_HEADLINES)

    need_vix      = cached_vix is None
    need_fii_dii  = cached_fii is None
    need_headlines = cached_headlines is None

    # ── Fetch missing data in parallel ───────────────────────────────────────
    if need_vix or need_fii_dii:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            tasks = []
            if need_vix:
                tasks.append(_fetch_india_vix(client))
            if need_fii_dii:
                tasks.append(_fetch_fii_dii(client))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            idx = 0
            if need_vix:
                vix_result = results[idx]; idx += 1
                if isinstance(vix_result, float):
                    cached_vix = str(vix_result)
                    await r.set(_KEY_VIX, cached_vix, ex=_TTL_NIFTY)
            if need_fii_dii:
                fii_result = results[idx]; idx += 1
                if isinstance(fii_result, tuple):
                    fii_net, dii_net = fii_result
                    if fii_net is not None:
                        cached_fii = str(fii_net)
                        await r.set(_KEY_FII_NET, cached_fii, ex=_TTL_FII)
                    if dii_net is not None:
                        cached_dii = str(dii_net)
                        await r.set(_KEY_DII_NET, cached_dii, ex=_TTL_FII)

    # Headlines and option metrics fetched in parallel (both have their own Redis caches)
    headlines_task = _fetch_headlines() if need_headlines else None
    from yukti.services.option_metrics_service import fetch_nifty_option_metrics
    option_task = fetch_nifty_option_metrics()

    headlines_result, option_metrics = await asyncio.gather(
        headlines_task if headlines_task else asyncio.sleep(0),
        option_task,
        return_exceptions=True,
    )

    if need_headlines and isinstance(headlines_result, list) and headlines_result:
        cached_headlines = "||".join(headlines_result)
        await r.set(_KEY_HEADLINES, cached_headlines, ex=_TTL_HEADLINES)

    # ── Populate context from cache ───────────────────────────────────────────
    if cached_vix:
        try:
            ctx.india_vix = float(cached_vix)
        except ValueError:
            pass

    if cached_fii:
        try:
            ctx.fii_net_cr = float(cached_fii)
        except ValueError:
            pass

    if cached_dii:
        try:
            ctx.dii_net_cr = float(cached_dii)
        except ValueError:
            pass

    if cached_headlines:
        ctx.headlines = [h.strip() for h in cached_headlines.split("||") if h.strip()]

    # ── Option chain metrics ──────────────────────────────────────────────────
    from yukti.services.option_metrics_service import OptionMetrics
    if isinstance(option_metrics, OptionMetrics):
        ctx.nifty_pcr         = option_metrics.pcr
        ctx.nifty_max_pain    = option_metrics.max_pain
        ctx.nifty_atm_iv      = option_metrics.atm_iv
        ctx.options_sentiment = option_metrics.sentiment

    log.debug(
        "MacroContext: Nifty %+.2f%% %s | VIX %s | FII %s | DII %s | PCR %s | %d headlines",
        ctx.nifty_chg_pct, ctx.nifty_trend, ctx.india_vix,
        ctx.fii_net_cr, ctx.dii_net_cr, ctx.nifty_pcr, len(ctx.headlines),
    )
    return ctx
