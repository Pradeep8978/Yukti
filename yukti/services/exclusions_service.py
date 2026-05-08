"""yukti/services/exclusions_service.py

Daily exclusion list — names a sane desk simply doesn't trade.

Three buckets, all hard exclusions (drop from candidate pool, don't just
demote):
  1. F&O ban list — NSE publishes per-day for SEBI position-limit breaches.
  2. ASM (additional surveillance measure) Stage 2+ / GSM (graded
     surveillance measure) — illiquid or manipulation-prone names.
  3. At-circuit names — opened lock-upper / lock-lower; no two-way market.

Fetches are best-effort. If NSE is unreachable we keep the previous Redis
cache (`yukti:exclusions:{date}`) rather than fail open. Callers can also
override via `settings.exclude_*` flags.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Iterable

import httpx

from yukti.config import settings

log = logging.getLogger(__name__)


_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
}

_FNO_BAN_URL = "https://www.nseindia.com/api/fiidiiTradeReact"   # placeholder
_ASM_URL     = "https://www.nseindia.com/api/reportASM"          # ASM long/short term
_GSM_URL     = "https://www.nseindia.com/api/reportGSM"          # GSM stages


@dataclass
class Exclusions:
    fno_ban: set[str] = field(default_factory=set)
    asm: set[str] = field(default_factory=set)         # stage 2+
    gsm: set[str] = field(default_factory=set)
    at_circuit: set[str] = field(default_factory=set)
    as_of: str = ""

    def reasons_for(self, symbol: str) -> list[str]:
        out: list[str] = []
        if symbol in self.fno_ban:    out.append("fno_ban")
        if symbol in self.asm:        out.append("asm")
        if symbol in self.gsm:        out.append("gsm")
        if symbol in self.at_circuit: out.append("at_circuit")
        return out

    def is_excluded(self, symbol: str) -> bool:
        if getattr(settings, "exclude_fno_ban",     True) and symbol in self.fno_ban:    return True
        if getattr(settings, "exclude_asm_gsm",     True) and (symbol in self.asm or symbol in self.gsm): return True
        if getattr(settings, "exclude_at_circuit",  True) and symbol in self.at_circuit: return True
        return False


# ── Fetchers (best-effort) ───────────────────────────────────────────────────

async def _fetch_json(url: str, prime: bool = True) -> dict | list | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            if prime:
                await client.get("https://www.nseindia.com", headers=_NSE_HEADERS)
            resp = await client.get(url, headers=_NSE_HEADERS)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        log.debug("Exclusions fetch failed for %s: %s", url, exc)
        return None


def _extract_symbols(payload, *paths) -> set[str]:
    """Pull symbol fields from heterogeneous NSE payloads."""
    if payload is None:
        return set()
    out: set[str] = set()
    rows = payload if isinstance(payload, list) else None
    if rows is None and isinstance(payload, dict):
        for p in paths:
            cur = payload
            for key in p:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(key)
            if isinstance(cur, list):
                rows = cur
                break
    rows = rows or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = (row.get("symbol") or row.get("Symbol") or row.get("sym") or "").strip().upper()
        if sym:
            out.add(sym)
    return out


async def refresh() -> Exclusions:
    """Refresh all exclusion buckets and persist to Redis under
    `yukti:exclusions:{YYYY-MM-DD}` (TTL 24h)."""
    today = date.today().isoformat()
    excl = Exclusions(as_of=today)

    if getattr(settings, "exclude_fno_ban", True):
        excl.fno_ban = _extract_symbols(await _fetch_json(_FNO_BAN_URL), ("data",), ("Banned",))

    if getattr(settings, "exclude_asm_gsm", True):
        asm_payload = await _fetch_json(_ASM_URL)
        gsm_payload = await _fetch_json(_GSM_URL)
        # ASM has long-term + short-term lists; treat both as Stage 2+ for
        # safety. If parsing fails, the set is just empty.
        excl.asm = _extract_symbols(asm_payload, ("data",), ("longterm", "data"))
        excl.gsm = _extract_symbols(gsm_payload, ("data",))

    # at-circuit is computed from the pre-market snapshot, see annotate_at_circuit()
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        await r.set(
            f"yukti:exclusions:{today}",
            json.dumps({k: sorted(list(v)) if isinstance(v, set) else v
                        for k, v in asdict(excl).items()}),
            ex=24 * 3600,
        )
        await r.set("yukti:exclusions:latest", today, ex=48 * 3600)
    except Exception as exc:
        log.warning("Exclusions Redis write failed: %s", exc)
    return excl


async def annotate_at_circuit_from_snapshot(tolerance_pct: float = 0.5) -> set[str]:
    """Walk the pre-market snapshot; if last == upper or lower band (within
    tolerance), mark the symbol at-circuit. Updates the cached Exclusions
    in Redis in place. Returns the at-circuit symbol set."""
    out: set[str] = set()
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        raw = await r.get("yukti:premarket:snapshot")
        if not raw:
            return out
        snap = json.loads(raw)
        for sym, q in snap.items():
            try:
                last = float(q.get("last", 0))
                prev = float(q.get("prev_close", 0))
                if last <= 0 or prev <= 0:
                    continue
                # Heuristic: if pre-open shows a ≥9% jump in either direction
                # we treat as likely-circuit (NSE bands are 5/10/20%).
                if abs((last - prev) / prev * 100) >= 9.0 - tolerance_pct:
                    out.add(sym.upper())
            except Exception:
                continue

        # Fold into today's exclusions cache
        today_key = await r.get("yukti:exclusions:latest")
        if today_key:
            cur = await r.get(f"yukti:exclusions:{today_key}")
            if cur:
                blob = json.loads(cur)
                blob["at_circuit"] = sorted(set(blob.get("at_circuit", [])) | out)
                await r.set(f"yukti:exclusions:{today_key}", json.dumps(blob), ex=24 * 3600)
    except Exception as exc:
        log.debug("at-circuit annotation skipped: %s", exc)
    return out


async def load_today() -> Exclusions:
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        today_key = await r.get("yukti:exclusions:latest")
        if not today_key:
            return Exclusions()
        raw = await r.get(f"yukti:exclusions:{today_key}")
        if not raw:
            return Exclusions()
        blob = json.loads(raw)
        return Exclusions(
            fno_ban=set(blob.get("fno_ban", [])),
            asm=set(blob.get("asm", [])),
            gsm=set(blob.get("gsm", [])),
            at_circuit=set(blob.get("at_circuit", [])),
            as_of=blob.get("as_of", ""),
        )
    except Exception:
        return Exclusions()


def filter_candidates(candidates: Iterable[dict], excl: Exclusions) -> tuple[list[dict], dict[str, int]]:
    """Return (kept, drop_counts_by_reason)."""
    kept: list[dict] = []
    drops: dict[str, int] = {}
    for c in candidates:
        sym = c["symbol"].upper()
        reasons = excl.reasons_for(sym)
        if reasons and excl.is_excluded(sym):
            for r in reasons:
                drops[r] = drops.get(r, 0) + 1
            continue
        kept.append(c)
    return kept, drops
