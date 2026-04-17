"""
tests/unit/test_risk.py
Unit tests for position sizing, SL/target calculation, and risk gates.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from yukti.risk import (
    Levels,
    PositionResult,
    calculate_levels,
    calculate_position,
)


# ── Position sizing tests ─────────────────────────────────────────────────────

class TestCalculatePosition:
    def test_standard_long(self):
        result = calculate_position(
            entry_price   = 1500.0,
            stop_loss     = 1470.0,   # ₹30 stop
            direction     = "LONG",
            conviction    = 8,
            account_value = 500_000,
            risk_pct      = 0.01,
        )
        # risk = 500k * 1% = 5000; stop = 30; base_qty = 166; mult 1.0 → 166
        assert result.quantity == 166
        assert result.stop_distance == 30.0
        assert result.risk_amount == 5000.0
        assert result.conviction_multiplier == 1.0

    def test_high_conviction_sizes_up(self):
        r8 = calculate_position(1500, 1470, "LONG", 8,  500_000, 0.01)
        r9 = calculate_position(1500, 1470, "LONG", 10, 500_000, 0.01)
        assert r9.quantity == int(r8.quantity * 1.5)

    def test_low_conviction_halves(self):
        r8 = calculate_position(1500, 1470, "LONG", 8, 500_000, 0.01)
        r6 = calculate_position(1500, 1470, "LONG", 6, 500_000, 0.01)
        assert r6.quantity == int(r8.quantity * 0.5)

    def test_conviction_below_5_returns_zero(self):
        result = calculate_position(1500, 1470, "LONG", 4, 500_000, 0.01)
        assert result.quantity == 0

    def test_short_direction(self):
        result = calculate_position(
            entry_price   = 1500.0,
            stop_loss     = 1535.0,   # short: SL above entry
            direction     = "SHORT",
            conviction    = 8,
            account_value = 500_000,
            risk_pct      = 0.01,
        )
        assert result.stop_distance == 35.0
        assert result.quantity > 0

    def test_invalid_stop_raises(self):
        with pytest.raises(ValueError):
            calculate_position(1500, 1550, "LONG", 8, 500_000, 0.01)


# ── SL / Target tests ─────────────────────────────────────────────────────────

class TestCalculateLevels:
    def test_long_atr_sl(self):
        levels = calculate_levels(
            direction   = "LONG",
            entry_price = 1500.0,
            atr         = 20.0,
            # No swing low → uses ATR-based SL
        )
        expected_sl = 1500 - 20 * 1.5   # 1470
        assert levels.stop_loss == pytest.approx(expected_sl, abs=0.01)

    def test_long_swing_low_tighter(self):
        levels = calculate_levels(
            direction   = "LONG",
            entry_price = 1500.0,
            atr         = 30.0,     # ATR-SL = 1500 - 45 = 1455
            swing_low   = 1475.0,   # swing-SL = 1475 * 0.995 = 1467.6
        )
        # Tighter (higher) = 1467.6, not 1455
        assert levels.stop_loss > 1455

    def test_long_targets_are_2r_and_3r(self):
        levels = calculate_levels("LONG", 1500, 20.0)
        stop_dist = 1500 - levels.stop_loss
        assert levels.target_1 == pytest.approx(1500 + stop_dist * 2, abs=0.01)
        assert levels.target_2 == pytest.approx(1500 + stop_dist * 3, abs=0.01)

    def test_short_sl_above_entry(self):
        levels = calculate_levels("SHORT", 1500.0, 20.0)
        assert levels.stop_loss > 1500

    def test_short_targets_below_entry(self):
        levels = calculate_levels("SHORT", 1500.0, 20.0)
        assert levels.target_1 < 1500
        assert levels.target_2 < levels.target_1

    def test_wide_stop_flagged(self):
        # stop > 2.5× ATR should be flagged as WIDE_STOP
        levels = calculate_levels(
            direction   = "LONG",
            entry_price = 1500.0,
            atr         = 10.0,
            swing_low   = 1450.0,   # stop = 50 = 5× ATR → wide
        )
        assert levels.entry_quality == "WIDE_STOP"

    def test_rr_is_2(self):
        levels = calculate_levels("LONG", 1500.0, 20.0)
        assert levels.risk_reward == pytest.approx(2.0)


# ── Gate tests (async) ────────────────────────────────────────────────────────

class TestGates:
    @pytest.mark.asyncio
    async def test_pass_all_gates(self):
        from yukti.risk import GateResult, run_gates

        pos = PositionResult(
            quantity=100, base_quantity=100, conviction_multiplier=1.0,
            risk_amount=5000, stop_distance=30, max_loss=3000,
            capital_deployed=150_000, capital_pct=30.0,
        )

        with (
            patch("yukti.risk.get_daily_pnl_pct", new=AsyncMock(return_value=0.5)),
            patch("yukti.risk.count_open_positions", new=AsyncMock(return_value=2)),
            patch("yukti.risk.is_on_cooldown", new=AsyncMock(return_value=False)),
        ):
            result = await run_gates("RELIANCE", "LONG", 2.5, pos, 500_000)

        assert result.passed is True

    @pytest.mark.asyncio
    async def test_blocked_by_daily_loss(self):
        from yukti.risk import run_gates

        pos = PositionResult(100, 100, 1.0, 5000, 30, 3000, 150_000, 30.0)

        with patch("yukti.risk.get_daily_pnl_pct", new=AsyncMock(return_value=-2.5)):
            result = await run_gates("RELIANCE", "LONG", 2.5, pos, 500_000)

        assert result.passed is False
        assert "daily_loss" in result.reason

    @pytest.mark.asyncio
    async def test_blocked_by_cooldown(self):
        from yukti.risk import run_gates

        pos = PositionResult(100, 100, 1.0, 5000, 30, 3000, 150_000, 30.0)

        with (
            patch("yukti.risk.get_daily_pnl_pct", new=AsyncMock(return_value=0.5)),
            patch("yukti.risk.count_open_positions", new=AsyncMock(return_value=1)),
            patch("yukti.risk.is_on_cooldown", new=AsyncMock(return_value=True)),
        ):
            result = await run_gates("RELIANCE", "LONG", 2.5, pos, 500_000)

        assert result.passed is False
        assert "cooldown" in result.reason

    @pytest.mark.asyncio
    async def test_blocked_by_low_rr(self):
        from yukti.risk import run_gates

        pos = PositionResult(100, 100, 1.0, 5000, 30, 3000, 150_000, 30.0)

        with (
            patch("yukti.risk.get_daily_pnl_pct", new=AsyncMock(return_value=0.0)),
            patch("yukti.risk.count_open_positions", new=AsyncMock(return_value=0)),
            patch("yukti.risk.is_on_cooldown", new=AsyncMock(return_value=False)),
        ):
            result = await run_gates("RELIANCE", "LONG", 1.2, pos, 500_000)

        assert result.passed is False
        assert "rr_too_low" in result.reason
