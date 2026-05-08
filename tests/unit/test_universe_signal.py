"""tests/unit/test_universe_signal.py — webhook HMAC + payload validation."""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch, AsyncMock

import pytest


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    """Mount only the universe_signal router on a minimal FastAPI app to
    avoid pulling the full Yukti app stack in unit tests."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr("yukti.config.settings.enable_webhook_signals", True, raising=False)
    monkeypatch.setattr("yukti.config.settings.webhook_hmac_secret", "shh-secret", raising=False)

    from yukti.api.routes.universe_signal import universe_signal_router
    app = FastAPI()
    app.include_router(universe_signal_router, prefix="/api")
    return TestClient(app)


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
            headers={"X-Yukti-Signature": _sign(body, "anything"), "Content-Type": "application/json"},
        )
        assert resp.status_code == 404

    def test_missing_signature_rejected(self, client):
        body = b'{"symbol":"RELIANCE","score_boost":5}'
        resp = client.post("/api/universe/signal", content=body,
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 401

    def test_bad_signature_rejected(self, client):
        body = b'{"symbol":"RELIANCE","score_boost":5}'
        resp = client.post(
            "/api/universe/signal", content=body,
            headers={
                "X-Yukti-Signature": "sha256=" + "a" * 64,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_valid_signature_accepted(self, client):
        body = b'{"symbol":"reliance","score_boost":7,"ttl_minutes":30,"source":"tradingview","note":"BO"}'

        # Stub Redis hset/expire — we don't need a real instance here.
        fake_redis = AsyncMock()
        with patch("yukti.data.state.get_redis", new=AsyncMock(return_value=fake_redis)):
            resp = client.post(
                "/api/universe/signal", content=body,
                headers={
                    "X-Yukti-Signature": _sign(body, "shh-secret"),
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
        assert data["symbol"] == "RELIANCE"   # uppercased
        assert data["expires_in_seconds"] == 30 * 60

        fake_redis.hset.assert_awaited_once()
        args, _ = fake_redis.hset.call_args
        assert args[0] == "yukti:scanner:boosts"
        assert args[1] == "RELIANCE"
        stored = json.loads(args[2])
        assert stored["score_boost"] == 7
        assert stored["source"] == "tradingview"

    def test_payload_validation_clamps_boost(self, client):
        # boost > 15 must fail validation (we only clamp 0..15 at the schema;
        # the scanner clamps to 10 at scoring time).
        body = b'{"symbol":"X","score_boost":99,"ttl_minutes":60}'
        resp = client.post(
            "/api/universe/signal", content=body,
            headers={
                "X-Yukti-Signature": _sign(body, "shh-secret"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 422

    def test_payload_validation_ttl_limits(self, client):
        body = b'{"symbol":"X","score_boost":1,"ttl_minutes":9999}'
        resp = client.post(
            "/api/universe/signal", content=body,
            headers={
                "X-Yukti-Signature": _sign(body, "shh-secret"),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 422
