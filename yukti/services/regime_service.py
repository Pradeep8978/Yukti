"""yukti/services/regime_service.py

Risk regime — what a desk does *to its sizing*, not its scoring.

Inputs:
  - India VIX (cached by the macro context service as `yukti:market:india_vix`).
  - Realised intraday drawdown (account_value vs day-start equity).
  - User-curated event days (`settings.event_days`, e.g. budget / RBI / FOMC).

Outputs (Redis `yukti:regime`, TTL 1h):
  {
    "bucket":           "calm" | "normal" | "elevated" | "stressed",
    "shortlist_depth":  0.30..1.00,    # fraction of scanner_pick_count to use
    "conviction_bump":  0..2,          # add to settings.min_conviction
    "preservation":     bool,          # intraday DD throttle on
    "event_day":        str | "",      # e.g. "RBI policy"
    "vix":              float | null,
  }

The scanner / market scan service / Arjun prompt all read this Redis blob.
No changes to risk gates here — they remain deterministic; regime only
*tightens* them via conviction bump and shortlist depth.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date

from yukti.config import settings

log = logging.getLogger(__name__)


@dataclass
class Regime:
    bucket: str = "normal"
    shortlist_depth: float = 1.0
    conviction_bump: int = 0
    preservation: bool = False
    event_day: str = ""
    vix: float | None = None


def classify_vix(vix: float | None, buckets: list[float] | None = None) -> tuple[str, float, int]:
    """Bucket VIX into (bucket, shortlist_depth, conviction_bump)."""
    bks = buckets or getattr(settings, "vix_regime_buckets", [12.0, 18.0, 25.0])
    if vix is None:
        return "normal", 1.0, 0
    if vix < bks[0]:
        return "calm", 1.0, 0
    if vix < bks[1]:
        return "normal", 1.0, 0
    if vix < bks[2]:
        return "elevated", 0.6, 1
    return "stressed", 0.3, 2


async def _read_vix() -> float | None:
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        raw = await r.get("yukti:market:india_vix")
        return float(raw) if raw else None
    except Exception:
        return None


async def _read_intraday_dd_pct() -> float:
    """Negative number (e.g. -0.012 for -1.2%) or 0.0 if unknown."""
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        raw = await r.get("yukti:state:daily_pnl_pct")
        if raw:
            return float(raw)
    except Exception:
        pass
    return 0.0


def _today_event() -> str:
    days = getattr(settings, "event_days", []) or []
    today = date.today().isoformat()
    for entry in days:
        # Entry can be "YYYY-MM-DD" or "YYYY-MM-DD:label"
        if isinstance(entry, str) and entry.startswith(today):
            parts = entry.split(":", 1)
            return parts[1] if len(parts) == 2 else "event"
    return ""


async def compute() -> Regime:
    vix = await _read_vix()
    bucket, depth, bump = classify_vix(vix)

    # Intraday drawdown throttle — preservation mode halves the shortlist and
    # adds a conviction bump if not already at the stressed bucket.
    dd = await _read_intraday_dd_pct()
    throttle_at = float(getattr(settings, "intraday_dd_throttle_pct", 0.01))
    preservation = dd <= -throttle_at
    if preservation:
        depth = min(depth, 0.5)
        bump = max(bump, 1)

    event = _today_event()
    if event:
        depth = min(depth, 0.5)
        bump = max(bump, 1)

    regime = Regime(
        bucket=bucket,
        shortlist_depth=round(depth, 2),
        conviction_bump=bump,
        preservation=preservation,
        event_day=event,
        vix=vix,
    )
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        await r.set("yukti:regime", json.dumps(asdict(regime)), ex=3600)
    except Exception as exc:
        log.debug("Regime Redis write failed: %s", exc)
    return regime


async def load() -> Regime:
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        raw = await r.get("yukti:regime")
        if not raw:
            return Regime()
        return Regime(**json.loads(raw))
    except Exception:
        return Regime()
