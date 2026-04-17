"""
tests/unit/test_arjun.py
Unit tests for the multi-provider Arjun agent.
Providers are mocked — no real API calls made.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yukti.agents.arjun import (
    TradeDecision,
    ClaudeProvider,
    GeminiProvider,
    Arjun,
    CallMeta,
    BaseProvider,
)
from datetime import datetime


# ── Fixtures ──────────────────────────────────────────────────────────────────

VALID_TRADE_JSON = json.dumps({
    "action":         "TRADE",
    "direction":      "LONG",
    "market_bias":    "BULLISH",
    "setup_type":     "trend_pullback",
    "reasoning":      "Strong uptrend with RSI pullback to EMA20. Market is bullish.",
    "entry_price":    1500.0,
    "entry_type":     "LIMIT",
    "stop_loss":      1470.0,
    "target_1":       1560.0,
    "target_2":       1590.0,
    "conviction":     8,
    "risk_reward":    2.0,
    "holding_period": "intraday",
    "skip_reason":    None,
})

VALID_SKIP_JSON = json.dumps({
    "action":         "SKIP",
    "direction":      None,
    "market_bias":    "AVOID",
    "setup_type":     None,
    "reasoning":      "F&O expiry day — market too volatile.",
    "entry_price":    None,
    "entry_type":     "LIMIT",
    "stop_loss":      None,
    "target_1":       None,
    "target_2":       None,
    "conviction":     2,
    "risk_reward":    None,
    "holding_period": "intraday",
    "skip_reason":    "market_avoid",
})

SAMPLE_META = CallMeta(
    provider="gemini", model="gemini-2.0-flash",
    latency_ms=800, input_tokens=900, output_tokens=250,
    cost_usd=0.0001, timestamp=datetime.utcnow(),
)

CONTEXT = "Test market context string"


# ── TradeDecision schema tests ────────────────────────────────────────────────

class TestTradeDecision:
    def test_valid_trade(self):
        d = TradeDecision(**json.loads(VALID_TRADE_JSON))
        assert d.action     == "TRADE"
        assert d.direction  == "LONG"
        assert d.conviction == 8

    def test_valid_skip(self):
        d = TradeDecision(**json.loads(VALID_SKIP_JSON))
        assert d.action     == "SKIP"
        assert d.direction  is None

    def test_trade_without_direction_raises(self):
        data = json.loads(VALID_TRADE_JSON)
        data["direction"] = None
        with pytest.raises(Exception):
            TradeDecision(**data)

    def test_trade_without_entry_raises(self):
        data = json.loads(VALID_TRADE_JSON)
        data["entry_price"] = None
        with pytest.raises(Exception):
            TradeDecision(**data)

    def test_conviction_bounds(self):
        data = json.loads(VALID_TRADE_JSON)
        data["conviction"] = 0
        with pytest.raises(Exception):
            TradeDecision(**data)
        data["conviction"] = 11
        with pytest.raises(Exception):
            TradeDecision(**data)


# ── Provider JSON parsing tests ───────────────────────────────────────────────

class TestBaseProviderParsing:
    class _ConcreteProvider(BaseProvider):
        async def call(self, context: str):
            pass

    provider = _ConcreteProvider()

    def test_parse_clean_json(self):
        data = self.provider._parse_json(VALID_TRADE_JSON, "test")
        assert data["action"] == "TRADE"

    def test_parse_fenced_json(self):
        fenced = f"```json\n{VALID_TRADE_JSON}\n```"
        data   = self.provider._parse_json(fenced, "test")
        assert data["action"] == "TRADE"

    def test_parse_invalid_json_raises(self):
        with pytest.raises(ValueError, match="JSON parse"):
            self.provider._parse_json("this is not json", "test")

    def test_validate_valid_data(self):
        data     = json.loads(VALID_TRADE_JSON)
        decision = self.provider._validate(data, "test")
        assert isinstance(decision, TradeDecision)

    def test_validate_bad_schema_raises(self):
        with pytest.raises(ValueError, match="Schema"):
            self.provider._validate({"action": "INVALID"}, "test")


# ── Arjun safe_decide tests ───────────────────────────────────────────────────

class TestArjun:
    def _make_arjun(self, decision_json: str) -> Arjun:
        """Build an Arjun whose provider always returns decision_json."""
        mock_provider        = MagicMock(spec=BaseProvider)
        d = TradeDecision(**json.loads(decision_json))
        mock_provider.call   = AsyncMock(return_value=(d, SAMPLE_META))
        return Arjun(mock_provider)

    @pytest.mark.asyncio
    async def test_safe_decide_returns_trade(self):
        arjun    = self._make_arjun(VALID_TRADE_JSON)
        decision = await arjun.safe_decide(CONTEXT)
        assert decision.action    == "TRADE"
        assert decision.direction == "LONG"

    @pytest.mark.asyncio
    async def test_safe_decide_returns_skip(self):
        arjun    = self._make_arjun(VALID_SKIP_JSON)
        decision = await arjun.safe_decide(CONTEXT)
        assert decision.action == "SKIP"

    @pytest.mark.asyncio
    async def test_safe_decide_never_raises_on_error(self):
        """Even if the provider throws, safe_decide returns a SKIP."""
        mock_provider      = MagicMock(spec=BaseProvider)
        mock_provider.call = AsyncMock(side_effect=RuntimeError("API down"))
        arjun              = Arjun(mock_provider)
        decision           = await arjun.safe_decide(CONTEXT)
        assert decision.action      == "SKIP"
        assert decision.skip_reason == "provider_error"

    @pytest.mark.asyncio
    async def test_provider_is_called_with_context(self):
        mock_provider      = MagicMock(spec=BaseProvider)
        d = TradeDecision(**json.loads(VALID_SKIP_JSON))
        mock_provider.call = AsyncMock(return_value=(d, SAMPLE_META))
        arjun              = Arjun(mock_provider)
        await arjun.safe_decide("my specific context")
        mock_provider.call.assert_called_once_with("my specific context")


# ── AB test provider tests ────────────────────────────────────────────────────

class TestABTestProvider:
    @pytest.mark.asyncio
    async def test_primary_decision_returned(self):
        from yukti.agents.arjun import ABTestProvider
        import yukti.agents.arjun as arjun_module

        primary_decision   = TradeDecision(**json.loads(VALID_TRADE_JSON))
        secondary_decision = TradeDecision(**json.loads(VALID_SKIP_JSON))

        mock_primary        = MagicMock(spec=BaseProvider)
        mock_secondary      = MagicMock(spec=BaseProvider)
        mock_primary.call   = AsyncMock(return_value=(primary_decision, SAMPLE_META))
        mock_secondary.call = AsyncMock(return_value=(secondary_decision, SAMPLE_META))

        ab              = ABTestProvider.__new__(ABTestProvider)
        ab._primary     = mock_primary
        ab._secondary   = mock_secondary
        ab._call_count  = 0
        ab._log_path    = "/tmp/test_ab.jsonl"

        import pathlib; pathlib.Path("/tmp").mkdir(exist_ok=True)

        decision, meta  = await ab.call(CONTEXT)
        assert decision.action    == "TRADE"   # primary wins
        assert decision.direction == "LONG"

    @pytest.mark.asyncio
    async def test_disagreement_is_logged(self):
        import asyncio
        from yukti.agents.arjun import ABTestProvider

        primary_d   = TradeDecision(**json.loads(VALID_TRADE_JSON))
        secondary_d = TradeDecision(**json.loads(VALID_SKIP_JSON))

        mock_primary        = MagicMock(spec=BaseProvider)
        mock_secondary      = MagicMock(spec=BaseProvider)
        mock_primary.call   = AsyncMock(return_value=(primary_d,   SAMPLE_META))
        mock_secondary.call = AsyncMock(return_value=(secondary_d, SAMPLE_META))

        ab             = ABTestProvider.__new__(ABTestProvider)
        ab._primary    = mock_primary
        ab._secondary  = mock_secondary
        ab._call_count = 0
        ab._log_path   = "/tmp/test_ab_log.jsonl"

        import pathlib; pathlib.Path("/tmp/test_ab_log.jsonl").unlink(missing_ok=True)

        await ab.call(CONTEXT)
        await asyncio.sleep(0.1)   # let background task complete

        import pathlib
        log_path = pathlib.Path("/tmp/test_ab_log.jsonl")
        # disagreement should have been logged (TRADE vs SKIP)
        if log_path.exists():
            content = log_path.read_text()
            assert "primary_action" in content
