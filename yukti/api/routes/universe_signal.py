"""yukti/api/routes/universe_signal.py

Inbound webhook for external scanner signals.

`POST /api/universe/signal` accepts an HMAC-SHA256-authenticated payload
from a TradingView alert / news provider / custom screener and parks a
score boost in Redis (`yukti:scanner:boosts`) for the scanner's next
cycle. Capped contribution (max 10) keeps a noisy webhook from
hijacking the deterministic score budget.

Auth (HMAC):
    Header `X-Yukti-Signature: sha256=<hex>` over the raw request body
    using `settings.webhook_hmac_secret`. We compare with `compare_digest`
    to avoid timing oracles.

Gated by `settings.enable_webhook_signals` — when off, the route 404s.

Payload schema (JSON):
    {
      "symbol":       "RELIANCE",     # required, will be uppercased
      "score_boost":  10,             # 0..15, clamped to 10 by scanner
      "ttl_minutes":  60,             # 1..240
      "source":       "tradingview",  # free-form tag, logged for audit
      "note":         "BO base"       # optional
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


def _verify_signature(raw_body: bytes, header_value: str | None) -> bool:
    secret = getattr(settings, "webhook_hmac_secret", "") or ""
    if not secret:
        return False
    if not header_value or "=" not in header_value:
        return False
    algo, _, hex_sig = header_value.partition("=")
    if algo.lower() != "sha256" or not hex_sig:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, hex_sig.lower())


@universe_signal_router.post("/signal")
async def submit_universe_signal(request: Request) -> dict[str, Any]:
    if not getattr(settings, "enable_webhook_signals", False):
        raise HTTPException(status_code=404, detail="Webhook signals disabled")

    raw = await request.body()
    sig = request.headers.get("X-Yukti-Signature")
    if not _verify_signature(raw, sig):
        raise HTTPException(status_code=401, detail="Invalid or missing signature")

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
        await r.hset("yukti:scanner:boosts", payload.symbol, blob)
        # TTL is on the hash key as a whole — refresh on every signal so
        # repeatedly-confirmed names persist; one-shot pings expire quickly.
        await r.expire("yukti:scanner:boosts", payload.ttl_minutes * 60)
    except Exception as exc:  # noqa: BLE001
        log.error("Webhook: Redis write failed: %s", exc)
        raise HTTPException(status_code=503, detail="Boost cache unavailable") from exc

    log.info(
        "Webhook signal accepted: symbol=%s boost=%.1f source=%s ttl=%dm",
        payload.symbol, payload.score_boost, payload.source, payload.ttl_minutes,
    )
    return {"accepted": True, "symbol": payload.symbol, "expires_in_seconds": payload.ttl_minutes * 60}
