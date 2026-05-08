"""
yukti/services/universe_scanner_service.py
Dynamic stock discovery engine.

Discovers tradeable stocks via 4 sources:
  1. Volume explosions (2x+ avg volume)
  2. Volatility breakouts (±2% close-to-close)
  3. News & events (catalysts from macro service headlines)
  4. Sector momentum (sectoral index moves)

Scores candidates 0-100, selects top N, writes to Redis.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from yukti.config import settings

log = logging.getLogger(__name__)

# ── Nifty 100 symbols (Nifty 50 + Next 50) for discovery pool ────────────────
# Security IDs from DhanHQ for NSE_EQ.
NIFTY_100_POOL: dict[str, str] = {
    "RELIANCE": "1333", "HDFCBANK": "1232", "INFY": "1594",
    "TCS": "11536", "ICICIBANK": "4963", "SBIN": "3045",
    "BHARTIARTL": "10604", "HINDUNILVR": "1394", "ITC": "1660",
    "KOTAKBANK": "1922", "LT": "11483", "AXISBANK": "5900",
    "BAJFINANCE": "317", "MARUTI": "10999", "TATAMOTORS": "3456",
    "SUNPHARMA": "3351", "NTPC": "11630", "ONGC": "2475",
    "WIPRO": "3787", "HCLTECH": "7229", "TATASTEEL": "3499",
    "ADANIENT": "25", "ADANIPORTS": "15083", "POWERGRID": "14977",
    "M&M": "2031", "ULTRACEMCO": "11532", "NESTLEIND": "17963",
    "TECHM": "13538", "BAJAJ-AUTO": "16669", "BAJAJFINSV": "16573",
    "JSWSTEEL": "11723", "TITAN": "3506", "DRREDDY": "881",
    "CIPLA": "694", "HINDALCO": "1363", "HEROMOTOCO": "1348",
    "BPCL": "526", "VEDL": "3063", "SHREECEM": "3103",
    "GRASIM": "1175", "COALINDIA": "20374", "DIVISLAB": "10940",
    "EICHERMOT": "14091", "ASIANPAINT": "236", "BRITANNIA": "547",
    "APOLLOHOSP": "157", "SBILIFE": "21808", "HDFCLIFE": "467",
    "INDUSINDBK": "5258", "DABUR": "772",
}

# ── Sector index mapping ─────────────────────────────────────────────────────
SECTOR_STOCKS: dict[str, list[str]] = {
    "BANK": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK"],
    "IT": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
    "PHARMA": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP"],
    "AUTO": ["TATAMOTORS", "MARUTI", "M&M", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT"],
    "METAL": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "COALINDIA"],
    "ENERGY": ["RELIANCE", "ONGC", "BPCL", "NTPC", "POWERGRID", "ADANIENT"],
}


# ═══════════════════════════════════════════════════════════════
#  SCORING (pure functions — no I/O, fully testable)
# ═══════════════════════════════════════════════════════════════

def _score_candidate(candidate: dict[str, Any]) -> float:
    """
    Score a discovery candidate 0-100.

    Recognised keys (all optional):
        vol_ratio          float — today's volume / 20-day avg
        change_pct         float — absolute close-to-close change %
        has_catalyst       bool  — news/event catalyst present
        sector_in_play     bool  — parent sector moving ±1.5%
        avg_turnover_cr    float — avg daily turnover in crores
        gap_pct            float — pre-market gap vs prev close (%)
        atr_pct_rank       float — 0..1 percentile of atr_pct within pool
        score_boost        float — additive boost (capped) from webhooks etc.

    The `volume_surge_threshold` and `price_move_threshold` settings act as
    *floors* — values below the threshold contribute zero to that component.
    Component budget (totals 100, missing fields → 0):
        vol(20) + price(15) + catalyst(15) + sector(10) + liq(10)
        + gap(10) + atr_rank(10) + boost(10) = 100
    """
    vol_ratio = candidate.get("vol_ratio", 0)
    change_pct = abs(candidate.get("change_pct", 0))
    has_catalyst = candidate.get("has_catalyst", False)
    catalyst_pts_override = candidate.get("catalyst_pts")  # 0..15 graded score
    sector_in_play = candidate.get("sector_in_play", False)
    avg_turnover_cr = candidate.get("avg_turnover_cr", 0)
    gap_pct = abs(candidate.get("gap_pct", 0) or 0)
    atr_rank = float(candidate.get("atr_pct_rank", 0) or 0)
    boost = float(candidate.get("score_boost", 0) or 0)

    vol_floor = float(getattr(settings, "volume_surge_threshold", 2.0))
    price_floor = float(getattr(settings, "price_move_threshold", 1.5))

    vol_score = min(vol_ratio / 5.0, 1.0) * 20 if vol_ratio >= vol_floor else 0
    price_score = min(change_pct / 4.0, 1.0) * 15 if change_pct >= price_floor else 0
    if catalyst_pts_override is not None:
        catalyst_score = max(0.0, min(float(catalyst_pts_override), 15.0))
    else:
        catalyst_score = 15 if has_catalyst else 0
    sector_score = 10 if sector_in_play else 0
    liq_score = min(avg_turnover_cr / 50.0, 1.0) * 10
    gap_score = min(gap_pct / 3.0, 1.0) * 10            # 3% gap saturates
    atr_score = max(0.0, min(atr_rank, 1.0)) * 10
    boost_score = max(0.0, min(boost, 10.0))

    total = (vol_score + price_score + catalyst_score + sector_score
             + liq_score + gap_score + atr_score + boost_score)
    return min(round(total, 1), 100)


def _annotate_atr_percentile(candidates: list[dict[str, Any]]) -> None:
    """In-place: rank atr_pct within the batch and store as `atr_pct_rank` (0..1)."""
    rows = [c for c in candidates if c.get("atr_pct_pool") is not None]
    if not rows:
        return
    sorted_vals = sorted(c["atr_pct_pool"] for c in rows)
    n = len(sorted_vals)
    if n == 1:
        rows[0]["atr_pct_rank"] = 1.0
        return
    for c in rows:
        # Higher atr_pct → higher rank (humans want names that move).
        idx = sorted_vals.index(c["atr_pct_pool"])
        c["atr_pct_rank"] = idx / (n - 1)


def _deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Deduplicate by symbol — keep the entry with the highest computed score.
    A stock found by multiple sources gets its highest score, not summed.
    """
    best: dict[str, dict[str, Any]] = {}
    for c in candidates:
        sym = c["symbol"]
        score = _score_candidate(c)
        if sym not in best or score > _score_candidate(best[sym]):
            best[sym] = c
    return list(best.values())


def _select_universe(
    candidates: list[dict[str, Any]],
    pick_count: int = 15,
    min_turnover_cr: float = 10,
    existing_positions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Apply liquidity floor, sort by score, pick top N.
    Always includes stocks with existing open positions.
    """
    # Liquidity filter — pinned (watchlist) names skip the floor.
    qualified = [
        c for c in candidates
        if c.get("pinned") or c.get("avg_turnover_cr", 0) >= min_turnover_cr
    ]

    # Score and sort
    scored = sorted(qualified, key=lambda c: _score_candidate(c), reverse=True)

    # Pick top N
    selected = scored[:pick_count]

    selected_symbols = {c["symbol"] for c in selected}

    # Always include pinned watchlist entries, regardless of score.
    for c in qualified:
        if c.get("pinned") and c["symbol"] not in selected_symbols:
            selected.append(c)
            selected_symbols.add(c["symbol"])

    # Ensure existing positions are included.
    if existing_positions:
        for c in qualified:
            if c["symbol"] in existing_positions and c["symbol"] not in selected_symbols:
                selected.append(c)
                selected_symbols.add(c["symbol"])

    return selected


# ═══════════════════════════════════════════════════════════════
#  DATA FETCHING (async, hits DhanHQ / Redis / news)
# ═══════════════════════════════════════════════════════════════

async def _fetch_volume_and_price_data(symbols: dict[str, str]) -> list[dict[str, Any]]:
    """
    Fetch previous-day candles for all symbols in the pool.
    Computes volume ratio and price change for each.
    """
    from yukti.execution.dhan_client import dhan

    candidates: list[dict[str, Any]] = []
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    for symbol, sec_id in symbols.items():
        try:
            raw = await dhan.get_candles(sec_id, "1", start, today)
            if not raw or len(raw) < 20:
                continue

            df = pd.DataFrame(
                raw, columns=["time", "open", "high", "low", "close", "volume"]
            ).astype({c: float for c in ["open", "high", "low", "close", "volume"]})

            vol_sma20 = df["volume"].rolling(20).mean().iloc[-1]
            vol_ratio = df["volume"].iloc[-1] / vol_sma20 if vol_sma20 > 0 else 0

            change_pct = 0.0
            if len(df) >= 2:
                change_pct = (df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2] * 100

            avg_turnover = (df["close"] * df["volume"]).rolling(20).mean().iloc[-1] / 1e7  # in crores

            candidates.append({
                "symbol": symbol,
                "security_id": sec_id,
                "vol_ratio": float(vol_ratio),
                "change_pct": float(change_pct),
                "has_catalyst": False,
                "sector_in_play": False,
                "avg_turnover_cr": float(avg_turnover),
            })
        except Exception as exc:
            log.warning("Scanner: failed to fetch %s: %s", symbol, exc)

    return candidates


async def _enrich_with_catalysts(
    candidates: list[dict[str, Any]],
    headlines: list[str],
) -> None:
    """Mark candidates that have news catalysts (in-place).

    Two paths:
      1. If the catalyst service is enabled, prefer per-symbol Redis cache
         (graded 0..15 score via NSE announcements + earnings calendar).
      2. Otherwise fall back to the legacy broad-market headline filter.
    """
    enabled = bool(getattr(settings, "enable_news_enrichment", True))
    if enabled:
        try:
            from yukti.services.catalyst_service import (
                get_items_for_symbol,
                is_pre_results,
                score_catalyst_for_symbol,
            )
            for c in candidates:
                items = await get_items_for_symbol(c["symbol"])
                pre_res = await is_pre_results(c["symbol"])
                pts = score_catalyst_for_symbol(c["symbol"], items, in_results_window=pre_res)
                if pts > 0:
                    c["catalyst_pts"] = pts
                    c["has_catalyst"] = True
                    if items:
                        c["catalyst_categories"] = sorted({i.category for i in items})
                    if pre_res:
                        c["pre_results"] = True
            return
        except Exception as exc:
            log.warning("Graded catalyst enrichment failed (%s) — using headline fallback", exc)

    # Legacy broad-market headline matching
    from yukti.services.macro_context_service import filter_headlines_for_symbol
    for c in candidates:
        matches = filter_headlines_for_symbol(c["symbol"], headlines)
        if matches:
            c["has_catalyst"] = True


async def _enrich_with_premarket_gap(candidates: list[dict[str, Any]]) -> None:
    """Read the pre-market snapshot written by `job_premarket_rank` and annotate
    each candidate with `gap_pct`. No-op if the cache is missing — the scanner
    still runs; gap simply contributes zero."""
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        raw = await r.get("yukti:premarket:snapshot")
        if not raw:
            return
        snap = json.loads(raw)
    except Exception as exc:
        log.debug("premarket snapshot unavailable: %s", exc)
        return
    for c in candidates:
        sym_data = snap.get(c["symbol"])
        if isinstance(sym_data, dict):
            c["gap_pct"] = float(sym_data.get("gap_pct") or 0)


async def _enrich_with_webhook_boosts(candidates: list[dict[str, Any]]) -> None:
    """Read non-expired webhook score boosts. Each symbol's boost lives in its
    own key (`yukti:scanner:boosts:{SYM}`) with an independent TTL — set by
    the webhook handler — so reads use a single MGET keyed on candidate
    symbols rather than scanning the full key space."""
    if not candidates:
        return
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        keys = [f"yukti:scanner:boosts:{c['symbol']}" for c in candidates]
        raws = await r.mget(keys)
        for c, raw in zip(candidates, raws):
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            c["score_boost"] = float(payload.get("score_boost") or 0)
            c["boost_source"] = payload.get("source")
    except Exception as exc:
        log.debug("webhook boost read failed: %s", exc)


async def _enrich_with_sector_momentum(
    candidates: list[dict[str, Any]],
) -> None:
    """
    Check sectoral momentum. If a sector moves ±1.5%, mark its stocks.
    Uses the candidates' own change_pct as a proxy for sector movement
    (average of sector members' changes).
    """
    sector_avg: dict[str, float] = {}
    for sector, members in SECTOR_STOCKS.items():
        changes = [
            c["change_pct"] for c in candidates
            if c["symbol"] in members and c.get("change_pct") is not None
        ]
        if changes:
            sector_avg[sector] = sum(changes) / len(changes)

    for c in candidates:
        for sector, members in SECTOR_STOCKS.items():
            if c["symbol"] in members:
                avg = sector_avg.get(sector, 0)
                if abs(avg) >= 1.5:
                    c["sector_in_play"] = True
                break


# ═══════════════════════════════════════════════════════════════
#  MAIN SCANNER SERVICE
# ═══════════════════════════════════════════════════════════════

async def _load_candidate_pool() -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Read the daily candidate pool from Redis (`yukti:candidate_pool`) and
    pin any symbols listed in `settings.watchlist`.

    Returns:
        symbols  — `{symbol: security_id}` to pass to data fetchers.
        meta     — `{symbol: {sector, turnover_cr, atr_pct, pinned}}` for
                   downstream scoring / hygiene gates.

    Falls back to the hardcoded `NIFTY_100_POOL` only if Redis has no entry —
    paper-mode developers shouldn't be blocked, but live/shadow operators are
    expected to populate the pool via the scheduler / CLI.
    """
    symbols: dict[str, str] = {}
    meta: dict[str, dict[str, Any]] = {}

    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        raw = await r.get("yukti:candidate_pool")
        if raw:
            pool = json.loads(raw)
            for entry in pool:
                sym = entry["symbol"]
                symbols[sym] = entry["security_id"]
                meta[sym] = {
                    "sector":      entry.get("sector", "Unknown"),
                    "turnover_cr": entry.get("turnover_cr"),
                    "atr_pct":     entry.get("atr_pct"),
                    "pinned":      False,
                }
            log.info("UniverseScanner: loaded candidate pool (%d symbols) from Redis", len(symbols))
    except Exception as exc:
        log.warning("UniverseScanner: could not read yukti:candidate_pool: %s", exc)

    if not symbols:
        log.warning(
            "UniverseScanner: candidate pool empty — falling back to built-in NIFTY_100_POOL "
            "(run `scripts/universe_loader.py --candidate-pool` to populate)"
        )
        symbols = dict(NIFTY_100_POOL)

    # Pin watchlist entries — they always make the pool, even with no metadata.
    for sym in (getattr(settings, "watchlist", []) or []):
        sym_u = sym.strip().upper()
        if not sym_u:
            continue
        if sym_u not in symbols:
            sec_id = NIFTY_100_POOL.get(sym_u, "")
            if not sec_id:
                log.warning("UniverseScanner: watchlist symbol %s not in pool/master — skipping", sym_u)
                continue
            symbols[sym_u] = sec_id
        meta.setdefault(sym_u, {"sector": "Unknown", "turnover_cr": None, "atr_pct": None})
        meta[sym_u]["pinned"] = True

    return symbols, meta


class UniverseScannerService:
    """
    Discovers stocks to trade. Runs at 08:45 (primary) and intraday refresh at 10:00, 12:00.
    Writes universe to Redis key `yukti:universe`.
    """

    def __init__(self) -> None:
        self._pool: dict[str, str] = {}
        self._pool_meta: dict[str, dict[str, Any]] = {}

    async def run_scan(self, is_refresh: bool = False) -> list[dict[str, str]]:
        """
        Execute a full discovery scan.

        Args:
            is_refresh: If True, merge new discoveries with existing universe (never remove).

        Returns:
            List of {symbol, security_id} dicts written to Redis.
        """
        log.info("UniverseScanner: starting %s scan", "refresh" if is_refresh else "primary")

        # 0. Refresh the candidate pool (daily build via scheduler / CLI; cached in Redis).
        self._pool, self._pool_meta = await _load_candidate_pool()

        # 1. Fetch volume + price data for the pool
        candidates = await _fetch_volume_and_price_data(self._pool)
        log.info("UniverseScanner: fetched data for %d symbols", len(candidates))

        # Carry sector/atr metadata from the pool through to candidates so the
        # rest of the pipeline (and downstream prompts) can see it.
        for c in candidates:
            m = self._pool_meta.get(c["symbol"])
            if m:
                c.setdefault("sector", m.get("sector"))
                c.setdefault("atr_pct_pool", m.get("atr_pct"))
                c["pinned"] = bool(m.get("pinned"))

        # 2. Enrich with catalysts
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            cached_headlines = await r.get("yukti:market:headlines")
            headlines = cached_headlines.split("||") if cached_headlines else []
        except Exception:
            headlines = []
        await _enrich_with_catalysts(candidates, headlines)

        # 3. Enrich with sector momentum
        await _enrich_with_sector_momentum(candidates)

        # 3b. Pre-market gap + ATR percentile + webhook boosts.
        await _enrich_with_premarket_gap(candidates)
        _annotate_atr_percentile(candidates)
        await _enrich_with_webhook_boosts(candidates)

        # 3c. Hard exclusions (F&O ban / ASM / GSM / at-circuit).
        try:
            from yukti.services.exclusions_service import load_today, filter_candidates
            excl = await load_today()
            kept, drops = filter_candidates(candidates, excl)
            if drops:
                log.info("Exclusions dropped: %s", drops)
            candidates = kept
        except Exception as exc:
            log.warning("Exclusions filter skipped: %s", exc)

        # 4. Deduplicate
        candidates = _deduplicate_candidates(candidates)

        # 5. Get existing positions (never remove mid-day)
        existing_positions: list[str] = []
        try:
            from yukti.data.state import get_all_positions
            positions = await get_all_positions()
            existing_positions = list(positions.keys())
        except Exception:
            pass

        # 6. Apply regime shortlist depth.
        try:
            from yukti.services.regime_service import compute as compute_regime
            regime = await compute_regime()
            depth = max(0.1, min(1.0, regime.shortlist_depth))
            effective_pick = max(3, int(round(settings.scanner_pick_count * depth)))
            log.info(
                "Regime: bucket=%s vix=%s depth=%.2f → pick_count %d→%d (preservation=%s, event=%s)",
                regime.bucket, regime.vix, depth, settings.scanner_pick_count,
                effective_pick, regime.preservation, regime.event_day,
            )
        except Exception as exc:
            log.debug("Regime compute skipped: %s", exc)
            effective_pick = settings.scanner_pick_count

        selected = _select_universe(
            candidates,
            pick_count=effective_pick,
            min_turnover_cr=settings.min_turnover_cr,
            existing_positions=existing_positions,
        )

        # 7. If refresh, merge with existing universe
        if is_refresh:
            selected = await self._merge_with_existing(selected)

        # 8. Write to Redis
        universe_list = [{"symbol": c["symbol"], "security_id": c["security_id"]} for c in selected]
        await self._write_to_redis(universe_list)

        # 9. Log scored results
        for c in selected:
            score = _score_candidate(c)
            log.info(
                "UniverseScanner: picked %s (score=%.1f, vol=%.1f×, chg=%.1f%%, catalyst=%s, sector=%s)",
                c["symbol"], score, c.get("vol_ratio", 0), c.get("change_pct", 0),
                c.get("has_catalyst"), c.get("sector_in_play"),
            )

        log.info("UniverseScanner: selected %d symbols", len(universe_list))
        return universe_list

    async def _merge_with_existing(self, new_picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge new discoveries with existing universe — never remove a stock mid-day."""
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            raw = await r.get("yukti:universe")
            if raw:
                existing = json.loads(raw)
                existing_symbols = {e["symbol"] for e in existing}
                merged_list = list(existing)
                for c in new_picks:
                    if c["symbol"] not in existing_symbols:
                        merged_list.append({"symbol": c["symbol"], "security_id": c["security_id"]})
                return [
                    next((p for p in new_picks if p["symbol"] == m["symbol"]), m)
                    for m in merged_list
                ]
        except Exception as exc:
            log.warning("UniverseScanner: merge failed: %s", exc)
        return new_picks

    async def _write_to_redis(self, universe_list: list[dict[str, str]]) -> None:
        """Write universe to Redis."""
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            await r.set("yukti:universe", json.dumps(universe_list))
            log.info("UniverseScanner: wrote %d symbols to yukti:universe", len(universe_list))
        except Exception as exc:
            log.error("UniverseScanner: Redis write failed: %s", exc)

    async def run_with_fallback(self, is_refresh: bool = False) -> list[dict[str, str]]:
        """
        Run scan with fallback chain:
        1. Full scan
        2. Previous session universe from Redis
        3. Emergency Nifty 50 baseline
        """
        try:
            return await self.run_scan(is_refresh=is_refresh)
        except Exception as exc:
            log.error("UniverseScanner: scan failed: %s — trying fallback", exc)

        # Fallback 1: previous session
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            raw = await r.get("yukti:universe")
            if raw:
                universe = json.loads(raw)
                log.warning("UniverseScanner: using previous session universe (%d symbols)", len(universe))
                return universe
        except Exception:
            pass

        # Fallback 2: emergency baseline
        log.warning("UniverseScanner: using emergency Nifty 50 baseline")
        baseline = [
            {"symbol": s, "security_id": sid}
            for s, sid in list(NIFTY_100_POOL.items())[:50]
        ]
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            await r.set("yukti:universe", json.dumps(baseline))
        except Exception:
            pass
        return baseline
