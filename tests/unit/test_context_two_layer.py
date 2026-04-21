"""tests/unit/test_context_two_layer.py — tests for two-layer context + alignment."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from yukti.signals.indicators import IndicatorSnapshot


def _make_snap(**overrides) -> IndicatorSnapshot:
    defaults = dict(
        close=1020.0, high=1025.0, low=1010.0, open=1015.0, volume=600_000,
        ema20=1010.0, ema50=1000.0, vwap=1012.0,
        supertrend=1005.0, supertrend_bull=True,
        rsi=58.0, macd=0.5, macd_sig=0.3, macd_hist=0.2, macd_bull=True,
        atr=15.0, bb_upper=1030.0, bb_mid=1010.0, bb_lower=990.0,
        volume_sma20=400_000, volume_ratio=1.5,
        trend="UPTREND", nearest_swing_high=1025.0, nearest_swing_low=990.0,
        prev_close=1010.0, candle_change_pct=1.0,
        adx=None, daily_support=None, daily_resistance=None,
    )
    defaults.update(overrides)
    return IndicatorSnapshot(**defaults)


def _make_macro(**overrides):
    macro = MagicMock()
    macro.nifty_chg_pct = overrides.get("nifty_chg_pct", 0.5)
    macro.nifty_trend = overrides.get("nifty_trend", "UP")
    macro.vix_label = overrides.get("vix_label", "15.0 (moderate)")
    macro.fii_label = overrides.get("fii_label", "+₹500 Cr (buying)")
    macro.dii_label = overrides.get("dii_label", "+₹300 Cr (buying)")
    macro.headlines_text = overrides.get("headlines_text", "  None available")
    return macro


def _make_perf(**overrides):
    defaults = {
        "consecutive_losses": 0, "daily_pnl_pct": 0.0,
        "win_rate_last_10": 0.6, "trades_today": 0,
    }
    defaults.update(overrides)
    return defaults


class TestAlignmentSignal:
    def test_compute_alignment_aligned(self):
        from yukti.signals.context import compute_alignment
        daily = _make_snap(trend="UPTREND")
        assert compute_alignment(daily, "LONG") == "ALIGNED"

    def test_compute_alignment_counter_trend(self):
        from yukti.signals.context import compute_alignment
        daily = _make_snap(trend="UPTREND")
        assert compute_alignment(daily, "SHORT") == "COUNTER-TREND"

    def test_compute_alignment_neutral(self):
        from yukti.signals.context import compute_alignment
        daily = _make_snap(trend="SIDEWAYS")
        assert compute_alignment(daily, "LONG") == "NEUTRAL"

    def test_compute_alignment_none_daily(self):
        from yukti.signals.context import compute_alignment
        assert compute_alignment(None, "LONG") == "NEUTRAL"


class TestTwoLayerContext:
    def test_daily_section_present(self):
        from yukti.signals.context import build_context

        snap_5m = _make_snap()
        snap_daily = _make_snap(
            trend="UPTREND", adx=32.0,
            daily_support=980.0, daily_resistance=1050.0,
        )
        ctx = build_context(
            "RELIANCE", snap_5m, _make_macro(), _make_perf(),
            indicators_daily=snap_daily,
        )
        assert "DAILY TIMEFRAME" in ctx
        assert "ADX" in ctx
        assert "Alignment" in ctx

    def test_orb_section_present(self):
        from yukti.signals.context import build_context

        snap_5m = _make_snap()
        ctx = build_context(
            "RELIANCE", snap_5m, _make_macro(), _make_perf(),
            or_high=1020.0, or_low=1000.0,
        )
        assert "Opening Range" in ctx

    def test_vwap_always_present(self):
        from yukti.signals.context import build_context

        snap_5m = _make_snap()
        ctx = build_context(
            "RELIANCE", snap_5m, _make_macro(), _make_perf(),
        )
        assert "VWAP" in ctx

    def test_backward_compatible_no_daily(self):
        from yukti.signals.context import build_context

        snap_5m = _make_snap()
        ctx = build_context("RELIANCE", snap_5m, _make_macro(), _make_perf())
        assert "STOCK: RELIANCE" in ctx
        # No daily section when not provided
        assert "DAILY TIMEFRAME" not in ctx
