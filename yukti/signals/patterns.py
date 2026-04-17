"""
yukti/signals/patterns.py
Detects concrete chart patterns from IndicatorSnapshot.
Each pattern returns a PatternSignal with detected=True/False, strength 0-1, and notes.
These feed into Claude's context as structured signals.
"""
from __future__ import annotations

from dataclasses import dataclass

from yukti.signals.indicators import IndicatorSnapshot


@dataclass
class PatternSignal:
    detected:     bool
    pattern_type: str
    strength:     float  # 0.0 → 1.0
    notes:        str


def breakout(snap: IndicatorSnapshot) -> PatternSignal:
    """
    Bullish breakout: price closes above recent swing high with volume surge.
    Classic setup: consolidation → expansion above resistance.
    """
    above_swing = snap.close > snap.nearest_swing_high
    vol_surge   = snap.volume_ratio > 1.5
    bull_trend  = snap.trend == "UPTREND" or snap.supertrend_bull
    macd_bull   = snap.macd_bull
    rsi_ok      = 50 < snap.rsi < 75  # not overbought yet

    score = sum([above_swing, vol_surge, bull_trend, macd_bull, rsi_ok])
    if score < 3 or not above_swing:
        return PatternSignal(False, "breakout", 0.0, "")

    strength = score / 5.0
    notes = (
        f"Price ₹{snap.close:.2f} > swing high ₹{snap.nearest_swing_high:.2f} "
        f"| vol {snap.volume_ratio:.1f}× avg"
        f"{' | MACD bull' if macd_bull else ''}"
        f"{' | RSI ' + str(round(snap.rsi, 1)) if rsi_ok else ''}"
    )
    return PatternSignal(True, "breakout", round(strength, 2), notes)


def breakdown(snap: IndicatorSnapshot) -> PatternSignal:
    """
    Bearish breakdown: price closes below recent swing low with volume surge.
    Entry for SHORT trades.
    """
    below_swing = snap.close < snap.nearest_swing_low
    vol_surge   = snap.volume_ratio > 1.5
    bear_trend  = snap.trend == "DOWNTREND" or not snap.supertrend_bull
    macd_bear   = not snap.macd_bull
    rsi_ok      = 25 < snap.rsi < 50

    score = sum([below_swing, vol_surge, bear_trend, macd_bear, rsi_ok])
    if score < 3 or not below_swing:
        return PatternSignal(False, "breakdown", 0.0, "")

    strength = score / 5.0
    notes = (
        f"Price ₹{snap.close:.2f} < swing low ₹{snap.nearest_swing_low:.2f} "
        f"| vol {snap.volume_ratio:.1f}× avg"
        f"{' | MACD bear' if macd_bear else ''}"
    )
    return PatternSignal(True, "breakdown", round(strength, 2), notes)


def trend_pullback_long(snap: IndicatorSnapshot) -> PatternSignal:
    """
    Pullback to EMA in an uptrend: buy the dip.
    Price retreats to EMA20/EMA50, RSI cools to 40-55 zone, then bounces.
    """
    uptrend       = snap.trend == "UPTREND" and snap.supertrend_bull
    near_ema20    = abs(snap.close - snap.ema20) / snap.ema20 < 0.008   # within 0.8%
    near_ema50    = abs(snap.close - snap.ema50) / snap.ema50 < 0.012
    at_ema        = near_ema20 or near_ema50
    rsi_cooled    = 38 <= snap.rsi <= 58
    above_vwap    = snap.above_vwap()
    macd_bull     = snap.macd_bull or snap.macd_hist > 0

    score = sum([uptrend, at_ema, rsi_cooled, above_vwap, macd_bull])
    if score < 3 or not uptrend or not at_ema:
        return PatternSignal(False, "trend_pullback", 0.0, "")

    which_ema = "EMA20" if near_ema20 else "EMA50"
    strength  = score / 5.0
    notes = (
        f"Uptrend pullback to {which_ema} ₹{snap.ema20 if near_ema20 else snap.ema50:.2f} "
        f"| RSI {snap.rsi:.1f} (cooled)"
        f"{' | above VWAP' if above_vwap else ''}"
    )
    return PatternSignal(True, "trend_pullback", round(strength, 2), notes)


def trend_pullback_short(snap: IndicatorSnapshot) -> PatternSignal:
    """
    Rally to EMA in a downtrend: sell the bounce.
    Price rallies up to EMA20/EMA50 in a downtrend, then rolls over.
    """
    downtrend    = snap.trend == "DOWNTREND" and not snap.supertrend_bull
    near_ema20   = abs(snap.close - snap.ema20) / snap.ema20 < 0.008
    near_ema50   = abs(snap.close - snap.ema50) / snap.ema50 < 0.012
    at_ema       = near_ema20 or near_ema50
    rsi_elevated = 42 <= snap.rsi <= 62
    below_vwap   = not snap.above_vwap()
    macd_bear    = not snap.macd_bull or snap.macd_hist < 0

    score = sum([downtrend, at_ema, rsi_elevated, below_vwap, macd_bear])
    if score < 3 or not downtrend or not at_ema:
        return PatternSignal(False, "trend_pullback_short", 0.0, "")

    which_ema = "EMA20" if near_ema20 else "EMA50"
    strength  = score / 5.0
    notes = (
        f"Downtrend rally to {which_ema} ₹{snap.ema20 if near_ema20 else snap.ema50:.2f} "
        f"| RSI {snap.rsi:.1f} (elevated)"
        f"{' | below VWAP' if below_vwap else ''}"
    )
    return PatternSignal(True, "trend_pullback_short", round(strength, 2), notes)


def reversal_long(snap: IndicatorSnapshot) -> PatternSignal:
    """
    Bullish reversal from oversold: RSI < 35, MACD turning up, at support.
    High risk / high reward — requires high conviction from Claude.
    """
    oversold      = snap.rsi < 36
    macd_turning  = snap.macd_hist > snap.macd_hist * 0.0  # hist > 0 or improving
    at_bb_lower   = snap.close < snap.bb_lower * 1.005     # near/below BB lower
    near_swing_lo = abs(snap.close - snap.nearest_swing_low) / snap.nearest_swing_low < 0.01
    candle_green  = snap.close > snap.open  # current candle is green

    score = sum([oversold, macd_turning, at_bb_lower, near_swing_lo, candle_green])
    if score < 3 or not oversold:
        return PatternSignal(False, "reversal_long", 0.0, "")

    strength = score / 5.0
    notes = (
        f"Oversold reversal: RSI {snap.rsi:.1f}"
        f"{' | near BB lower' if at_bb_lower else ''}"
        f"{' | at swing low ₹' + str(round(snap.nearest_swing_low, 2)) if near_swing_lo else ''}"
        f"{' | green candle' if candle_green else ''}"
    )
    return PatternSignal(True, "reversal_long", round(strength, 2), notes)


def reversal_short(snap: IndicatorSnapshot) -> PatternSignal:
    """
    Bearish reversal from overbought: RSI > 65, at resistance, MACD rolling over.
    """
    overbought    = snap.rsi > 64
    at_bb_upper   = snap.close > snap.bb_upper * 0.995
    near_swing_hi = abs(snap.close - snap.nearest_swing_high) / snap.nearest_swing_high < 0.01
    candle_red    = snap.close < snap.open
    macd_hist_neg = snap.macd_hist < 0

    score = sum([overbought, at_bb_upper, near_swing_hi, candle_red, macd_hist_neg])
    if score < 3 or not overbought:
        return PatternSignal(False, "reversal_short", 0.0, "")

    strength = score / 5.0
    notes = (
        f"Overbought reversal: RSI {snap.rsi:.1f}"
        f"{' | at BB upper' if at_bb_upper else ''}"
        f"{' | near swing high ₹' + str(round(snap.nearest_swing_high, 2)) if near_swing_hi else ''}"
        f"{' | red candle' if candle_red else ''}"
    )
    return PatternSignal(True, "reversal_short", round(strength, 2), notes)


def momentum_long(snap: IndicatorSnapshot) -> PatternSignal:
    """
    Strong bullish momentum: everything aligned — trend, MACD, RSI, volume, VWAP.
    Chase only with confirmation; best on strong opening moves.
    """
    rsi_momentum  = 58 <= snap.rsi <= 72
    macd_bull     = snap.macd_bull and snap.macd_hist > 0
    above_vwap    = snap.above_vwap()
    above_ema20   = snap.above_ema20()
    vol_surge     = snap.volume_ratio > 1.3
    supertrend    = snap.supertrend_bull

    score = sum([rsi_momentum, macd_bull, above_vwap, above_ema20, vol_surge, supertrend])
    if score < 4:
        return PatternSignal(False, "momentum", 0.0, "")

    strength = score / 6.0
    notes = (
        f"Momentum: RSI {snap.rsi:.1f}"
        f" | MACD hist {snap.macd_hist:+.3f}"
        f" | vol {snap.volume_ratio:.1f}×"
        f" | {'above' if above_vwap else 'below'} VWAP"
    )
    return PatternSignal(True, "momentum", round(strength, 2), notes)


def scan_all(snap: IndicatorSnapshot) -> list[PatternSignal]:
    """
    Run all pattern detectors and return detected signals sorted by strength.
    """
    all_patterns = [
        breakout(snap),
        breakdown(snap),
        trend_pullback_long(snap),
        trend_pullback_short(snap),
        reversal_long(snap),
        reversal_short(snap),
        momentum_long(snap),
    ]
    detected = [p for p in all_patterns if p.detected]
    return sorted(detected, key=lambda p: p.strength, reverse=True)


def best_pattern(snap: IndicatorSnapshot) -> PatternSignal | None:
    """Return the single highest-strength detected pattern, or None."""
    found = scan_all(snap)
    return found[0] if found else None
