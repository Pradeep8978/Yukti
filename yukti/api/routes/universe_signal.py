"""yukti/api/routes/universe_signal.py

Inbound webhook for external scanner signals.

`POST /api/universe/signal` accepts an HMAC-SHA256-authenticated payload
from a TradingView alert / news provider / custom screener and parks a
score boost in Redis (`yukti:scanner:boosts:{symbol}`) for the scanner's
next cycle. Capped contribution (max 10) keeps a noisy webhook from
hijacking the deterministic score budget.

Auth (HMAC):
    Header `X-Yukti-Signature: sha256=<hex>` over `<timestamp>.<body>` using
    `settings.webhook_hmac_secret`. Header `X-Yukti-Timestamp: <unix-seconds>`
    must be within ±300s of server time. Comparison uses `compare_digest`.

    Replay protection: each signed request must use a unique
    `X-Yukti-Nonce` (any string ≤64 chars). Nonces are tracked in Redis
    with a TTL matching the timestamp tolerance window.

Gated by `settings.enable_webhook_signals` — when off, the route 404s.

Storage:
    One key per symbol: `yukti:scanner:boosts:{SYM}` with per-key TTL set
    to the payload's `ttl_minutes`. Earlier high-TTL boosts are no longer
    truncated by later low-TTL ones.

Payload schema (JSON):
    {
      "symbol":       "RELIANCE",
      "score_boost":  10,
      "ttl_minutes":  60,
      "source":       "tradingview",
      "note":         "BO base"
    }
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from yukti.config import settings

log = logging.getLogger(__name__)

universe_signal_router = APIRouter(prefix="/universe", tags=["universe"])

_TIMESTAMP_TOLERANCE_SECONDS = 300
_NONCE_TTL_SECONDS = _TIMESTAMP_TOLERANCE_SECONDS * 2  # cover the full window
_BOOST_KEY_PREFIX = "yukti:scanner:boosts:"


class UniverseSignal(BaseModel):
    symbol: str
    score_boost: float = Field(ge=0.0, le=15.0)
    ttl_minutes: int = Field(ge=1, le=240, default=60)
    source: str = Field(default="external", max_length=64)
    note: str | None = Field(default=None, max_length=400)

    @field_validator("symbol")
    @classmethod
    def normalise_symbol(cls, v: str) -> str:
        v2 = (v or "").strip().upper()
        if not v2:
            raise ValueError("symbol is required")
        return v2


def _verify_signature(
    raw_body: bytes,
    header_value: str | None,
    timestamp: str | None,
) -> bool:
    """HMAC-SHA256 over `<timestamp>.<body>`. Both header and timestamp must
    be present and in the expected formats."""
    secret = getattr(settings, "webhook_hmac_secret", "") or ""
    if not secret or not header_value or not timestamp:
        return False
    if "=" not in header_value:
        return False
    algo, _, hex_sig = header_value.partition("=")
    if algo.lower() != "sha256" or not hex_sig:
        return False
    signed_payload = timestamp.encode() + b"." + raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, hex_sig.lower())


def _verify_timestamp(timestamp: str | None) -> bool:
    if not timestamp:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    return abs(int(time.time()) - ts) <= _TIMESTAMP_TOLERANCE_SECONDS


async def _claim_nonce(nonce: str | None) -> bool:
    """Return True if the nonce was previously unseen (and is now claimed),
    False if it has been used recently (replay)."""
    if not nonce or len(nonce) > 64:
        return False
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        # SET key value NX EX <ttl> — atomic claim. Returns truthy on success.
        ok = await r.set(f"yukti:webhook:nonce:{nonce}", "1",
                         nx=True, ex=_NONCE_TTL_SECONDS)
        return bool(ok)
    except Exception as exc:
        log.warning("Webhook nonce claim failed: %s", exc)
        # Fail closed on Redis errors — a missing nonce store would let
        # replays through silently.
        return False


@universe_signal_router.post("/signal")
async def submit_universe_signal(request: Request) -> dict[str, Any]:
    if not getattr(settings, "enable_webhook_signals", False):
        raise HTTPException(status_code=404, detail="Webhook signals disabled")

    raw = await request.body()
    sig = request.headers.get("X-Yukti-Signature")
    ts = request.headers.get("X-Yukti-Timestamp")
    nonce = request.headers.get("X-Yukti-Nonce")

    if not _verify_timestamp(ts):
        raise HTTPException(status_code=401, detail="Invalid or expired timestamp")
    if not _verify_signature(raw, sig, ts):
        raise HTTPException(status_code=401, detail="Invalid or missing signature")
    if not await _claim_nonce(nonce):
        raise HTTPException(status_code=401, detail="Missing or replayed nonce")

    try:
        payload = UniverseSignal.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Bad payload: {exc}") from exc

    blob = json.dumps({
        "score_boost": payload.score_boost,
        "source":      payload.source,
        "note":        payload.note,
        "ts":          int(time.time()),
    })
    try:
        from yukti.data.state import get_redis
        r = await get_redis()
        # Per-key TTL — each symbol's boost expires independently. A 5-min
        # ping no longer truncates a 60-min boost on another symbol.
        await r.set(
            f"{_BOOST_KEY_PREFIX}{payload.symbol}",
            blob,
            ex=payload.ttl_minutes * 60,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Webhook: Redis write failed: %s", exc)
        raise HTTPException(status_code=503, detail="Boost cache unavailable") from exc

    log.info(
        "Webhook signal accepted: symbol=%s boost=%.1f source=%s ttl=%dm",
        payload.symbol, payload.score_boost, payload.source, payload.ttl_minutes,
    )
    return {"accepted": True, "symbol": payload.symbol, "expires_in_seconds": payload.ttl_minutes * 60}
