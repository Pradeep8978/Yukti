"""tests/unit/test_universe_signal.py — webhook HMAC + payload validation."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import patch, AsyncMock

import pytest


def _sign(body: bytes, secret: str, ts: str | None = None) -> tuple[str, str]:
    """Return (signature_header, timestamp). HMAC over `<ts>.<body>`."""
    ts = ts or str(int(time.time()))
    payload = ts.encode() + b"." + body
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return sig, ts


def _signed_headers(body: bytes, secret: str) -> dict[str, str]:
    sig, ts = _sign(body, secret)
    return {
        "X-Yukti-Signature": sig,
        "X-Yukti-Timestamp": ts,
        "X-Yukti-Nonce": uuid.uuid4().hex,
        "Content-Type": "application/json",
    }


@pytest.fixture
def client(monkeypatch):
    """Mount only the universe_signal router on a minimal FastAPI app to
    avoid pulling the full Yukti app stack in unit tests."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr("yukti.config.settings.enable_webhook_signals", True, raising=False)
    monkeypatch.setattr("yukti.config.settings.webhook_hmac_secret", "shh-secret", raising=False)

    # Default Redis stub: nonces are always fresh (set NX → True), boost
    # writes are accepted. Individual tests can override.
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "yukti.data.state.get_redis",
        AsyncMock(return_value=fake_redis),
        raising=False,
    )

    from yukti.api.routes.universe_signal import universe_signal_router
    app = FastAPI()
    app.include_router(universe_signal_router, prefix="/api")
    tc = TestClient(app)
    tc.fake_redis = fake_redis  # type: ignore[attr-defined]
    return tc


class TestWebhook:
    def test_disabled_returns_404(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        monkeypatch.setattr("yukti.config.settings.enable_webhook_signals", False, raising=False)
        from yukti.api.routes.universe_signal import universe_signal_router
        app = FastAPI()
        app.include_router(universe_signal_router, prefix="/api")
        c = TestClient(app)
        body = b'{"symbol":"X","score_boost":1}'
        resp = c.post(
            "/api/universe/signal",
            content=body,
            headers=_signed_headers(body, "anything"),
        )
        assert resp.status_code == 404

    def test_missing_signature_rejected(self, client):
        body = b'{"symbol":"RELIANCE","score_boost":5}'
        resp = client.post("/api/universe/signal", content=body,
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 401

    def test_bad_signature_rejected(self, client):
        body = b'{"symbol":"RELIANCE","score_boost":5}'
        ts = str(int(time.time()))
        resp = client.post(
            "/api/universe/signal", content=body,
            headers={
                "X-Yukti-Signature": "sha256=" + "a" * 64,
                "X-Yukti-Timestamp": ts,
                "X-Yukti-Nonce": uuid.uuid4().hex,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_stale_timestamp_rejected(self, client):
        body = b'{"symbol":"RELIANCE","score_boost":5}'
        old_ts = str(int(time.time()) - 3600)
        sig, _ = _sign(body, "shh-secret", ts=old_ts)
        resp = client.post(
            "/api/universe/signal", content=body,
            headers={
                "X-Yukti-Signature": sig,
                "X-Yukti-Timestamp": old_ts,
                "X-Yukti-Nonce": uuid.uuid4().hex,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_replayed_nonce_rejected(self, client, monkeypatch):
        # Force the nonce-claim Redis SET NX to return False (replay).
        client.fake_redis.set = AsyncMock(return_value=False)
        body = b'{"symbol":"RELIANCE","score_boost":5}'
        resp = client.post(
            "/api/universe/signal", content=body,
            headers=_signed_headers(body, "shh-secret"),
        )
        assert resp.status_code == 401

    def test_valid_signature_accepted(self, client):
        body = b'{"symbol":"reliance","score_boost":7,"ttl_minutes":30,"source":"tradingview","note":"BO"}'
        # First call (nonce claim): True; subsequent SET (boost write) needs
        # to return a truthy value too. AsyncMock default returns True per call.
        client.fake_redis.set = AsyncMock(return_value=True)
        resp = client.post(
            "/api/universe/signal", content=body,
            headers=_signed_headers(body, "shh-secret"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
        assert data["symbol"] == "RELIANCE"
        assert data["expires_in_seconds"] == 30 * 60

        # Two SET calls: one for the nonce, one for the boost.
        assert client.fake_redis.set.await_count == 2
        # Find the boost write (the call whose key starts with the boost prefix).
        boost_call = next(
            c for c in client.fake_redis.set.await_args_list
            if c.args and c.args[0].startswith("yukti:scanner:boosts:")
        )
        assert boost_call.args[0] == "yukti:scanner:boosts:RELIANCE"
        stored = json.loads(boost_call.args[1])
        assert stored["score_boost"] == 7
        assert stored["source"] == "tradingview"
        assert boost_call.kwargs.get("ex") == 30 * 60

    def test_payload_validation_clamps_boost(self, client):
        body = b'{"symbol":"X","score_boost":99,"ttl_minutes":60}'
        resp = client.post(
            "/api/universe/signal", content=body,
            headers=_signed_headers(body, "shh-secret"),
        )
        assert resp.status_code == 422

    def test_payload_validation_ttl_limits(self, client):
        body = b'{"symbol":"X","score_boost":1,"ttl_minutes":9999}'
        resp = client.post(
            "/api/universe/signal", content=body,
            headers=_signed_headers(body, "shh-secret"),
        )
        assert resp.status_code == 422
