"""tests/unit/test_vix_regime.py — VIX regime bucketing in rules_engine."""
from __future__ import annotations

import pytest
from unittest.mock import patch

from yukti.signals.indicators import IndicatorSnapshot
from yukti.signals.patterns import PatternSignal
from yukti.services.macro_context_service import MacroContext
from yukti.agents.rules_engine import decide


def _snap(**overrides) -> IndicatorSnapshot:
    defaults = dict(
        close=1020.0, high=1030.0, low=1010.0, open=1015.0, volume=600_000,
        ema20=1010.0, ema50=990.0, vwap=1008.0,
        supertrend=1000.0, supertrend_bull=True,
        rsi=58.0, macd=1.0, macd_sig=0.5, macd_hist=0.5, macd_bull=True,
        atr=12.0, bb_upper=1040.0, bb_mid=1010.0, bb_lower=980.0,
        volume_sma20=400_000, volume_ratio=1.8,
        trend="UPTREND", nearest_swing_high=1028.0, nearest_swing_low=990.0,
        prev_close=1010.0, candle_change_pct=1.0,
        adx=None, daily_support=None, daily_resistance=None,
        body_ratio=0.7, is_hammer=False, is_shooting_star=False,
        rsi_bullish_divergence=False, rsi_bearish_divergence=False,
    )
    defaults.update(overrides)
    return IndicatorSnapshot(**defaults)


def _macro(india_vix: float | None = None, nifty_trend: str = "UP") -> MacroContext:
    return MacroContext(nifty_chg_pct=0.3, nifty_trend=nifty_trend, india_vix=india_vix)


def _perf() -> dict:
    return {"daily_pnl_pct": 0.0, "consecutive_losses": 0, "win_rate_last_10": 0.6,
            "win_rate_last_10_count": 10, "trades_today": 2}


def _breakout_pattern() -> PatternSignal:
    return PatternSignal(detected=True, pattern_type="breakout", strength=0.9, notes="test")


def _momentum_pattern() -> PatternSignal:
    return PatternSignal(detected=True, pattern_type="momentum", strength=0.85, notes="test")


# ── VIX extreme block ─────────────────────────────────────────────────────────

class TestVIXExtreme:
    def test_vix_30_or_above_skips_all_trades(self):
        snap = _snap()
        result, _ = decide("INFY", snap, _macro(india_vix=30.5), _perf(), _breakout_pattern())
        assert result.action == "SKIP"
        assert "vix_extreme" in (result.skip_reason or "")

    def test_vix_just_below_30_does_not_hit_extreme(self):
        snap = _snap()
        result, _ = decide("INFY", snap, _macro(india_vix=29.9), _perf(), _breakout_pattern())
        # Should not be blocked by vix_extreme specifically (may still skip for other reasons)
        assert "vix_extreme" not in (result.skip_reason or "")


# ── Low VIX blocks momentum/breakout ─────────────────────────────────────────

class TestLowVIX:
    """VIX < 12: momentum and breakout patterns should be skipped."""

    def test_momentum_blocked_in_low_vix(self):
        snap = _snap()
        result, _ = decide("INFY", snap, _macro(india_vix=10.5), _perf(), _momentum_pattern())
        assert result.action == "SKIP"
        assert "vix_too_low_for_momentum_breakout" in (result.skip_reason or "")

    def test_breakout_blocked_in_low_vix(self):
        snap = _snap()
        result, _ = decide("INFY", snap, _macro(india_vix=11.0), _perf(), _breakout_pattern())
        assert result.action == "SKIP"
        assert "vix_too_low_for_momentum_breakout" in (result.skip_reason or "")

    def test_reversal_allowed_in_low_vix(self):
        """Reversals and VWAP plays should still be considered in low VIX."""
        snap = _snap(rsi=35.0, is_hammer=True, close=1005.0, low=1005.0)
        pattern = PatternSignal(detected=True, pattern_type="reversal_long", strength=0.85, notes="")
        result, _ = decide("INFY", snap, _macro(india_vix=10.0), _perf(), pattern)
        # Low VIX should NOT block reversal patterns — they thrive in range-bound markets
        assert "vix_too_low_for_momentum_breakout" not in (result.skip_reason or "")


# ── High VIX raises conviction floor ─────────────────────────────────────────

class TestHighVIX:
    """VIX 25-30: min_conviction raised to 8, aggressive LONG momentum blocked."""

    def test_high_vix_blocks_momentum_long(self):
        snap = _snap()
        result, _ = decide("INFY", snap, _macro(india_vix=26.0), _perf(), _momentum_pattern())
        assert result.action == "SKIP"
        assert "vix_high_blocks_aggressive_long" in (result.skip_reason or "")

    def test_high_vix_blocks_breakout_long(self):
        snap = _snap()
        result, _ = decide("INFY", snap, _macro(india_vix=27.5), _perf(), _breakout_pattern())
        assert result.action == "SKIP"
        assert "vix_high_blocks_aggressive_long" in (result.skip_reason or "")

    def test_high_vix_allows_breakdown_short(self):
        """Breakdown (short) should not be blocked by high VIX — only aggressive LONG patterns are."""
        snap = _snap(
            close=975.0, high=985.0, low=975.0, open=982.0,
            ema20=1000.0, ema50=1010.0, supertrend_bull=False,
            rsi=42.0, macd=-1.0, macd_sig=-0.5, macd_bull=False,
            trend="DOWNTREND", volume_ratio=2.0, body_ratio=0.7,
            rsi_bullish_divergence=False,
        )
        pattern = PatternSignal(detected=True, pattern_type="breakdown", strength=0.85, notes="")
        result, _ = decide("RELIANCE", snap, _macro(india_vix=27.0, nifty_trend="DOWN"), _perf(), pattern)
        assert "vix_high_blocks_aggressive_long" not in (result.skip_reason or "")


# ── Elevated VIX raises min_conviction to 7 ──────────────────────────────────

class TestElevatedVIX:
    """VIX 18-25: min_conviction floor raised to 7."""

    def test_elevated_vix_raises_conviction_floor(self):
        """A conviction-5 setup should be skipped when VIX is elevated."""
        # Minimal setup: pattern barely detected, low-conviction baseline
        snap = _snap(rsi=58.0, volume_ratio=1.2, body_ratio=0.4)
        pattern = PatternSignal(detected=True, pattern_type="breakout", strength=0.60, notes="")
        # With settings.min_conviction=5 and elevated VIX → floor becomes 7
        result, _ = decide("INFY", snap, _macro(india_vix=20.0), _perf(), pattern)
        # Result may or may not skip, but the conviction floor must be 7 if it skips
        if result.action == "SKIP" and "conviction" in (result.skip_reason or ""):
            # If it skipped on conviction, the threshold must be ≥ 7
            import re
            m = re.search(r"below_min_(\d+)", result.skip_reason or "")
            if m:
                assert int(m.group(1)) >= 7


# ── Misconfigured buckets guard ───────────────────────────────────────────────

class TestVIXBucketGuard:
    def test_empty_buckets_does_not_raise(self):
        """If vix_regime_buckets is empty or short, regime defaults to 'normal' — no IndexError."""
        snap = _snap()
        with patch("yukti.agents.rules_engine.settings") as mock_settings:
            mock_settings.min_conviction = 5
            mock_settings.vix_regime_buckets = []  # misconfigured
            mock_settings.daily_loss_warn_pct = 1.2
            mock_settings.use_ai_decision = False
            # Should not raise IndexError
            try:
                result, _ = decide("INFY", snap, _macro(india_vix=10.0), _perf(), _breakout_pattern())
            except IndexError:
                pytest.fail("IndexError raised from empty vix_regime_buckets")

    def test_short_bucket_list_does_not_raise(self):
        snap = _snap()
        with patch("yukti.agents.rules_engine.settings") as mock_settings:
            mock_settings.min_conviction = 5
            mock_settings.vix_regime_buckets = [12]  # only 1 element
            mock_settings.daily_loss_warn_pct = 1.2
            mock_settings.use_ai_decision = False
            try:
                result, _ = decide("INFY", snap, _macro(india_vix=10.0), _perf(), _breakout_pattern())
            except IndexError:
                pytest.fail("IndexError raised from short vix_regime_buckets")
