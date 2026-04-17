"""
tests/integration/test_trade_cycle.py
Integration tests for the end-to-end trade cycle using the paper broker.
These tests run the full pipeline (indicators → Claude → risk → paper fill)
against synthetic OHLCV data. No real DhanHQ or Anthropic calls are made —
Claude is mocked to return deterministic decisions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from yukti.agents.arjun import TradeDecision
from yukti.backtest.paper_broker import PaperBroker, SimPosition
from yukti.signals.indicators import compute


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_trending_df(n: int = 80) -> pd.DataFrame:
    rng    = np.random.default_rng(1)
    closes = 1500 * np.cumprod(1 + rng.normal(0.002, 0.004, n))
    highs  = closes * (1 + abs(rng.normal(0, 0.002, n)))
    lows   = closes * (1 - abs(rng.normal(0, 0.002, n)))
    opens  = np.roll(closes, 1); opens[0] = 1500.0
    vols   = rng.normal(600_000, 80_000, n).clip(1)
    df = pd.DataFrame({
        "open": opens, "high": highs,
        "low": lows, "close": closes, "volume": vols,
    })
    df.index = pd.date_range("2024-06-01 09:15", periods=n, freq="5min")
    return df


_LONG_DECISION = TradeDecision(
    action         = "TRADE",
    direction      = "LONG",
    market_bias    = "BULLISH",
    setup_type     = "trend_pullback",
    reasoning      = "Strong uptrend with RSI pullback to 50. Market is bullish.",
    entry_price    = 1550.0,
    entry_type     = "LIMIT",
    stop_loss      = 1525.0,
    target_1       = 1600.0,
    target_2       = 1625.0,
    conviction     = 8,
    risk_reward    = 2.0,
    holding_period = "intraday",
)

_SHORT_DECISION = TradeDecision(
    action         = "TRADE",
    direction      = "SHORT",
    market_bias    = "BEARISH",
    setup_type     = "breakdown",
    reasoning      = "Breaking below swing low. Market weak.",
    entry_price    = 1480.0,
    entry_type     = "LIMIT",
    stop_loss      = 1505.0,
    target_1       = 1430.0,
    target_2       = 1405.0,
    conviction     = 7,
    risk_reward    = 2.0,
    holding_period = "intraday",
)

_SKIP_DECISION = TradeDecision(
    action      = "SKIP",
    reasoning   = "Market is AVOID — F&O expiry day, no clear direction.",
    skip_reason = "market_avoid",
    conviction  = 3,
)


# ── Paper broker unit tests ───────────────────────────────────────────────────

class TestPaperBroker:
    @pytest.mark.asyncio
    async def test_place_order_returns_fill(self):
        broker = PaperBroker(500_000)
        broker._prices["RELIANCE"] = 1550.0

        resp = await broker.place_order(
            security_id="RELIANCE", transaction_type="BUY",
            quantity=100, order_type="LIMIT",
            product_type="INTRADAY", price=1550.0,
        )
        assert resp["orderStatus"] == "TRADED"
        assert resp["filledQty"]   == 100
        assert resp["averagePrice"] > 1550.0   # slippage applied

    @pytest.mark.asyncio
    async def test_gtt_triggers_on_sl_breach(self):
        broker = PaperBroker(500_000)
        broker._prices["RELIANCE"] = 1550.0

        pos = SimPosition(
            symbol="RELIANCE", direction="LONG", quantity=100,
            entry_price=1550.0, stop_loss=1520.0, target_1=1600.0,
            holding="intraday",
        )
        broker.register_position("RELIANCE", pos)

        await broker.place_gtt(
            security_id="RELIANCE", transaction_type="SELL",
            quantity=100, trigger_price=1520.0,
            order_type="SL-M", product_type="INTRADAY",
        )

        broker.update_price("RELIANCE", 1518.0)   # breach SL
        assert "RELIANCE" not in broker._positions
        assert len(broker.closed_trades) == 1
        assert broker.closed_trades[0].exit_reason == "stop_loss_hit"

    @pytest.mark.asyncio
    async def test_gtt_triggers_on_target(self):
        broker = PaperBroker(500_000)
        broker._prices["RELIANCE"] = 1550.0

        pos = SimPosition(
            symbol="RELIANCE", direction="LONG", quantity=100,
            entry_price=1550.0, stop_loss=1520.0, target_1=1600.0,
            holding="intraday",
        )
        broker.register_position("RELIANCE", pos)

        await broker.place_gtt(
            security_id="RELIANCE", transaction_type="SELL",
            quantity=100, trigger_price=1600.0,
            order_type="LIMIT", product_type="INTRADAY", price=1600.0,
        )

        broker.update_price("RELIANCE", 1602.0)   # hits target
        assert len(broker.closed_trades) == 1
        assert broker.closed_trades[0].exit_reason == "target_1_hit"

    @pytest.mark.asyncio
    async def test_pnl_updates_account_value(self):
        broker = PaperBroker(500_000)
        broker._prices["TCS"] = 3500.0

        pos = SimPosition(
            symbol="TCS", direction="LONG", quantity=50,
            entry_price=3500.0, stop_loss=3450.0, target_1=3600.0,
            holding="intraday",
        )
        broker.register_position("TCS", pos)
        broker._close_position("TCS", 3600.0, "target_1_hit")

        expected_pnl = (3600.0 - 3500.0) * 50   # ₹5,000
        assert broker.account_value == pytest.approx(500_000 + expected_pnl)

    @pytest.mark.asyncio
    async def test_short_pnl_correct(self):
        broker = PaperBroker(500_000)
        pos = SimPosition(
            symbol="INFY", direction="SHORT", quantity=200,
            entry_price=1500.0, stop_loss=1530.0, target_1=1450.0,
            holding="intraday",
        )
        broker.register_position("INFY", pos)
        broker._close_position("INFY", 1450.0, "target_1_hit")

        expected_pnl = (1500.0 - 1450.0) * 200   # ₹10,000
        assert broker.account_value == pytest.approx(500_000 + expected_pnl)


# ── Risk gates integration ────────────────────────────────────────────────────

class TestRiskGatesIntegration:
    @pytest.mark.asyncio
    async def test_full_pass(self):
        from yukti.risk import calculate_position, run_gates

        with (
            patch("yukti.risk.get_daily_pnl_pct", new=AsyncMock(return_value=0.5)),
            patch("yukti.risk.count_open_positions", new=AsyncMock(return_value=1)),
            patch("yukti.risk.is_on_cooldown", new=AsyncMock(return_value=False)),
        ):
            pos = calculate_position(1550, 1525, "LONG", 8, 500_000, 0.01)
            gate = await run_gates("RELIANCE", "LONG", 2.0, pos, 500_000)

        assert gate.passed is True
        assert pos.quantity > 0

    @pytest.mark.asyncio
    async def test_blocked_all_positions_full(self):
        from yukti.risk import calculate_position, run_gates

        with (
            patch("yukti.risk.get_daily_pnl_pct", new=AsyncMock(return_value=0.0)),
            patch("yukti.risk.count_open_positions", new=AsyncMock(return_value=5)),
            patch("yukti.risk.is_on_cooldown", new=AsyncMock(return_value=False)),
        ):
            pos  = calculate_position(1550, 1525, "LONG", 8, 500_000, 0.01)
            gate = await run_gates("RELIANCE", "LONG", 2.0, pos, 500_000)

        assert gate.passed is False
        assert "max_positions" in gate.reason


# ── Indicator snapshot integration ───────────────────────────────────────────

class TestIndicatorIntegration:
    def test_compute_on_trend_df(self):
        df   = _make_trending_df(80)
        snap = compute(df)
        assert snap.trend == "UPTREND"
        assert snap.close > 0
        assert snap.atr > 0
        assert 0 < snap.rsi < 100

    def test_compute_requires_min_candles(self):
        df = _make_trending_df(10)
        # Should not raise, but some indicators will be NaN → filled to 0
        snap = compute(df)
        assert snap is not None
