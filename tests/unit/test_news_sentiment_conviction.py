"""tests/unit/test_news_sentiment_conviction.py

Tests the Step 4c news sentiment conviction vote in rules_engine.decide().

MacroContext doesn't have a catalyst_items field by default — the rules engine
reads it via getattr(..., None) so it fails open when absent. Here we attach
it directly to a MacroContext instance to simulate what the live scanner does
before calling decide().
"""
from __future__ import annotations

import pytest
from yukti.signals.indicators import IndicatorSnapshot
from yukti.signals.patterns import PatternSignal
from yukti.services.macro_context_service import MacroContext
from yukti.services.news_provider import NewsItem
from yukti.agents.rules_engine import decide


# ── Shared fixtures ───────────────────────────────────────────────────────────

def _snap(**overrides) -> IndicatorSnapshot:
    """Strong snap — used for hard-skip tests where we don't care about baseline level."""
    defaults = dict(
        close=1020.0, high=1030.0, low=1010.0, open=1015.0, volume=600_000,
        ema20=1010.0, ema50=990.0, vwap=1008.0,
        supertrend=1000.0, supertrend_bull=True,
        rsi=58.0, macd=1.0, macd_sig=0.5, macd_hist=0.5, macd_bull=True,
        atr=12.0, bb_upper=1040.0, bb_mid=1010.0, bb_lower=980.0,
        volume_sma20=400_000, volume_ratio=1.8,
        trend="UPTREND", nearest_swing_high=1028.0, nearest_swing_low=990.0,
        prev_close=1010.0, candle_change_pct=1.0,
        adx=25.0, daily_support=None, daily_resistance=None,
        body_ratio=0.6, is_hammer=False, is_shooting_star=False,
        rsi_bullish_divergence=False, rsi_bearish_divergence=False,
    )
    defaults.update(overrides)
    return IndicatorSnapshot(**defaults)


def _mid_snap(**overrides) -> IndicatorSnapshot:
    """Medium-strength snap — baseline conviction ~7 so bonuses (+1/+2) and
    penalties (-2) are visible without hitting the 10-cap.

    Bonuses active:  supertrend_bull (+1), above_ema20 (+1)
    Bonuses stripped: vol neutral (1.0×), below_vwap_long (-1), macd_bull=False (0)
    Pattern strength 0.65 (no pattern bonus/penalty)
    Nifty SIDEWAYS (no sector tailwind)
    Net baseline = 6 + 1(supertrend) - 1(below_vwap) + 1(above_ema20) = 7
    """
    defaults = dict(
        close=1020.0, high=1030.0, low=1010.0, open=1015.0, volume=500_000,
        ema20=1010.0, ema50=990.0, vwap=1025.0,    # close < vwap → -1 below_vwap_long
        supertrend=1000.0, supertrend_bull=True,    # trend_confirmed gate requires this
        rsi=55.0, macd=0.2, macd_sig=0.5, macd_hist=-0.3, macd_bull=False,  # no MACD bonus
        atr=12.0, bb_upper=1040.0, bb_mid=1010.0, bb_lower=980.0,
        volume_sma20=500_000, volume_ratio=1.0,     # no vol_high bonus
        trend="UPTREND", nearest_swing_high=1028.0, nearest_swing_low=990.0,
        prev_close=1010.0, candle_change_pct=1.0,
        adx=25.0, daily_support=None, daily_resistance=None,
        body_ratio=0.6, is_hammer=False, is_shooting_star=False,
        rsi_bullish_divergence=False, rsi_bearish_divergence=False,
    )
    defaults.update(overrides)
    return IndicatorSnapshot(**defaults)


def _mid_pattern() -> PatternSignal:
    """Pattern strength 0.65 — no pattern_strong bonus, no pattern_weak penalty."""
    return PatternSignal(detected=True, pattern_type="reversal_long", strength=0.65, notes="test")


def _macro_sideways_no_news() -> MacroContext:
    """SIDEWAYS Nifty → no sector tailwind bonus."""
    return MacroContext(nifty_chg_pct=0.0, nifty_trend="SIDEWAYS", india_vix=15.0)


def _macro_sideways_with_news(symbol: str, sentiment: float) -> MacroContext:
    macro = MacroContext(nifty_chg_pct=0.0, nifty_trend="SIDEWAYS", india_vix=15.0)
    item = NewsItem(symbol=symbol, ts="2026-05-17T09:00:00Z", category="RESULTS",
                    headline="test", sentiment=sentiment)
    macro.catalyst_items = {symbol: [item]}
    return macro


def _macro_with_news(symbol: str, sentiment: float, nifty_trend: str = "UP") -> MacroContext:
    """MacroContext with catalyst_items pre-populated for symbol."""
    macro = MacroContext(nifty_chg_pct=0.3, nifty_trend=nifty_trend, india_vix=15.0)
    item = NewsItem(
        symbol=symbol,
        ts="2026-05-17T09:00:00Z",
        category="RESULTS",
        headline="test headline",
        sentiment=sentiment,
    )
    macro.catalyst_items = {symbol: [item]}
    return macro


def _macro_no_news(nifty_trend: str = "UP") -> MacroContext:
    """MacroContext with no catalyst_items (normal case — field absent)."""
    return MacroContext(nifty_chg_pct=0.3, nifty_trend=nifty_trend, india_vix=15.0)


def _perf() -> dict:
    return {"daily_pnl_pct": 0.0, "consecutive_losses": 0, "win_rate_last_10": 0.6,
            "win_rate_last_10_count": 10, "trades_today": 2}


def _reversal_long() -> PatternSignal:
    return PatternSignal(detected=True, pattern_type="reversal_long", strength=0.75, notes="test")


# ── No news: vote must not fire ───────────────────────────────────────────────

class TestNoNews:
    def test_no_catalyst_items_field_does_not_crash(self):
        result, _ = decide("INFY", _snap(), _macro_no_news(), _perf(), _reversal_long())
        # Should reach a normal decision (not crash or skip on news)
        assert "news" not in (result.skip_reason or "")

    def test_empty_catalyst_items_does_not_fire(self):
        macro = _macro_no_news()
        macro.catalyst_items = {}          # field present but empty
        result, _ = decide("INFY", _snap(), macro, _perf(), _reversal_long())
        assert "news" not in (result.skip_reason or "")

    def test_symbol_absent_from_catalyst_dict_does_not_fire(self):
        macro = _macro_no_news()
        macro.catalyst_items = {"OTHER": []}   # different symbol
        result, _ = decide("INFY", _snap(), macro, _perf(), _reversal_long())
        assert "news" not in (result.skip_reason or "")


# ── Strongly negative news: hard skip ────────────────────────────────────────

class TestStronglyNegativeNews:
    """Sentiment ≤ -0.5 must produce a hard skip regardless of technicals."""

    def test_sebi_level_sentiment_skips(self):
        result, _ = decide("INFY", _snap(), _macro_with_news("INFY", -0.85), _perf(), _reversal_long())
        assert result.action == "SKIP"
        assert "news_strongly_negative" in (result.skip_reason or "")

    def test_exactly_minus_0_5_skips(self):
        result, _ = decide("INFY", _snap(), _macro_with_news("INFY", -0.5), _perf(), _reversal_long())
        assert result.action == "SKIP"
        assert "news_strongly_negative" in (result.skip_reason or "")

    def test_minus_0_49_does_not_hard_skip(self):
        result, _ = decide("INFY", _snap(), _macro_with_news("INFY", -0.49), _perf(), _reversal_long())
        # -0.49 falls into the mild negative band (penalty, not hard skip)
        assert "news_strongly_negative" not in (result.skip_reason or "")


# ── Mildly negative news: conviction penalty ─────────────────────────────────

class TestMildlyNegativeNews:
    """Sentiment in (-0.5, -0.2] applies -2 to conviction score."""

    def test_mild_negative_reduces_conviction(self):
        result_clean, _ = decide("INFY", _mid_snap(), _macro_sideways_no_news(), _perf(), _mid_pattern())
        result_neg,   _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",-0.35), _perf(), _mid_pattern())
        assert result_neg.conviction < result_clean.conviction

    def test_mild_negative_causes_conviction_skip(self):
        # -2 penalty drops conviction from 7 → 5, below min_conviction=7 → skip
        result, _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",-0.35), _perf(), _mid_pattern())
        assert result.action == "SKIP"
        assert result.conviction == 5

    def test_neutral_band_minus_0_19_no_penalty(self):
        result_clean, _ = decide("INFY", _mid_snap(), _macro_sideways_no_news(), _perf(), _mid_pattern())
        result_neut,  _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",-0.19), _perf(), _mid_pattern())
        assert result_neut.conviction == result_clean.conviction


# ── Strongly positive news: conviction bonus ─────────────────────────────────

class TestStronglyPositiveNews:
    """Sentiment ≥ 0.5 applies +2 to conviction score."""

    def test_strong_positive_raises_conviction(self):
        result_clean, _ = decide("INFY", _mid_snap(), _macro_sideways_no_news(), _perf(), _mid_pattern())
        result_pos,   _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",0.75), _perf(), _mid_pattern())
        assert result_pos.conviction > result_clean.conviction

    def test_exactly_0_5_gets_strong_bonus(self):
        result_clean, _ = decide("INFY", _mid_snap(), _macro_sideways_no_news(), _perf(), _mid_pattern())
        result_pos,   _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",0.5), _perf(), _mid_pattern())
        assert result_pos.conviction > result_clean.conviction

    def test_strong_positive_reason_present(self):
        result, _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",0.75), _perf(), _mid_pattern())
        assert "news_strongly_positive" in result.reasoning


# ── Mildly positive news: smaller conviction bonus ───────────────────────────

class TestMildlyPositiveNews:
    """Sentiment in [0.2, 0.5) applies +1 to conviction score."""

    def test_mild_positive_raises_conviction_by_one(self):
        result_clean, _ = decide("INFY", _mid_snap(), _macro_sideways_no_news(), _perf(), _mid_pattern())
        result_pos,   _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",0.30), _perf(), _mid_pattern())
        assert result_pos.conviction == result_clean.conviction + 1

    def test_mild_positive_reason_present(self):
        result, _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",0.30), _perf(), _mid_pattern())
        assert "news_positive" in result.reasoning

    def test_strong_beats_mild_bonus(self):
        result_mild,   _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",0.30), _perf(), _mid_pattern())
        result_strong, _ = decide("INFY", _mid_snap(), _macro_sideways_with_news("INFY",0.75), _perf(), _mid_pattern())
        assert result_strong.conviction > result_mild.conviction


# ── Worst-of-batch: multiple items, worst score governs ──────────────────────

class TestMultipleNewsItems:
    """When a symbol has multiple news items, the worst sentiment governs."""

    def test_mixed_news_worst_governs_long_skip(self):
        # LONG: worst = -0.85 → hard skip even though one item is strongly positive
        macro = MacroContext(nifty_chg_pct=0.3, nifty_trend="UP", india_vix=15.0)
        macro.catalyst_items = {"INFY": [
            NewsItem("INFY", "2026-05-17T09:00:00Z", "RESULTS", "beats estimates", sentiment=0.80),
            NewsItem("INFY", "2026-05-17T09:00:00Z", "OTHER",   "SEBI issues notice", sentiment=-0.85),
        ]}
        result, _ = decide("INFY", _snap(), macro, _perf(), _reversal_long())
        assert result.action == "SKIP"
        assert "news_strongly_negative" in (result.skip_reason or "")

    def test_two_positive_items_still_gets_bonus(self):
        macro = MacroContext(nifty_chg_pct=0.0, nifty_trend="SIDEWAYS", india_vix=15.0)
        macro.catalyst_items = {"INFY": [
            NewsItem("INFY", "2026-05-17T09:00:00Z", "RESULTS",  "record profit", sentiment=0.90),
            NewsItem("INFY", "2026-05-17T09:00:00Z", "GUIDANCE", "order win",     sentiment=0.60),
        ]}
        result_clean, _ = decide("INFY", _mid_snap(), _macro_sideways_no_news(), _perf(), _mid_pattern())
        result_pos,   _ = decide("INFY", _mid_snap(), macro, _perf(), _mid_pattern())
        assert result_pos.conviction > result_clean.conviction


# ── Short trades: negative news should HELP, not block ───────────────────────

def _bear_snap(**overrides) -> IndicatorSnapshot:
    """Bearish mid-strength snap for short tests.
    Baseline: 6 + 1(supertrend_bear) + 1(below_ema20) - 1(above_vwap_short) = 7
    """
    defaults = dict(
        close=980.0, high=988.0, low=975.0, open=986.0, volume=500_000,
        ema20=1000.0, ema50=1010.0, vwap=975.0,     # close > vwap → -1 above_vwap_short
        supertrend=1005.0, supertrend_bull=False,    # supertrend_bear → +1 + trend_confirmed
        rsi=42.0, macd=-0.2, macd_sig=-0.1, macd_hist=-0.1, macd_bull=False,
        atr=12.0, bb_upper=1020.0, bb_mid=1000.0, bb_lower=980.0,
        volume_sma20=500_000, volume_ratio=1.0,
        trend="DOWNTREND", nearest_swing_high=1000.0, nearest_swing_low=970.0,
        prev_close=986.0, candle_change_pct=-0.6,
        adx=25.0, daily_support=None, daily_resistance=None,
        body_ratio=0.6, is_hammer=False, is_shooting_star=False,
        rsi_bullish_divergence=False, rsi_bearish_divergence=False,
    )
    defaults.update(overrides)
    return IndicatorSnapshot(**defaults)


def _breakdown_pattern() -> PatternSignal:
    """Breakdown SHORT pattern, strength 0.65 (no pattern bonus/penalty)."""
    return PatternSignal(detected=True, pattern_type="breakdown", strength=0.65, notes="test")


def _macro_down_no_news() -> MacroContext:
    return MacroContext(nifty_chg_pct=-0.3, nifty_trend="DOWN", india_vix=15.0)


def _macro_down_with_news(symbol: str, sentiment: float) -> MacroContext:
    macro = MacroContext(nifty_chg_pct=-0.3, nifty_trend="DOWN", india_vix=15.0)
    item = NewsItem(symbol=symbol, ts="2026-05-17T09:00:00Z", category="OTHER",
                    headline="test", sentiment=sentiment)
    macro.catalyst_items = {symbol: [item]}
    return macro


class TestShortWithNegativeNews:
    """Strongly negative news should boost short conviction, not block it."""

    def test_strongly_negative_news_boosts_short(self):
        result_clean, _ = decide("INFY", _bear_snap(), _macro_down_no_news(), _perf(), _breakdown_pattern())
        result_short, _ = decide("INFY", _bear_snap(), _macro_down_with_news("INFY", -0.85), _perf(), _breakdown_pattern())
        assert result_short.conviction > result_clean.conviction

    def test_strongly_negative_news_short_not_skipped(self):
        result, _ = decide("INFY", _bear_snap(), _macro_down_with_news("INFY", -0.85), _perf(), _breakdown_pattern())
        assert "news_strongly_negative" not in (result.skip_reason or "")

    def test_strongly_negative_news_confirms_short_in_reasoning(self):
        result, _ = decide("INFY", _bear_snap(), _macro_down_with_news("INFY", -0.85), _perf(), _breakdown_pattern())
        assert "news_strongly_negative_confirms_short" in result.reasoning

    def test_mildly_negative_news_boosts_short(self):
        result_clean, _ = decide("INFY", _bear_snap(), _macro_down_no_news(), _perf(), _breakdown_pattern())
        result_short, _ = decide("INFY", _bear_snap(), _macro_down_with_news("INFY", -0.35), _perf(), _breakdown_pattern())
        assert result_short.conviction > result_clean.conviction

    def test_strongly_positive_news_penalises_short(self):
        result_clean, _ = decide("INFY", _bear_snap(), _macro_down_no_news(), _perf(), _breakdown_pattern())
        result_short, _ = decide("INFY", _bear_snap(), _macro_down_with_news("INFY", 0.75), _perf(), _breakdown_pattern())
        assert result_short.conviction < result_clean.conviction

    def test_mildly_positive_news_penalises_short(self):
        result_clean, _ = decide("INFY", _bear_snap(), _macro_down_no_news(), _perf(), _breakdown_pattern())
        result_short, _ = decide("INFY", _bear_snap(), _macro_down_with_news("INFY", 0.30), _perf(), _breakdown_pattern())
        assert result_short.conviction < result_clean.conviction

    def test_long_still_blocked_on_strongly_negative(self):
        # Regression guard: LONG hard-skip is unaffected by this change
        result, _ = decide("INFY", _snap(), _macro_with_news("INFY", -0.85), _perf(), _reversal_long())
        assert result.action == "SKIP"
        assert "news_strongly_negative" in (result.skip_reason or "")
