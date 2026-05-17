"""
yukti/signals/patterns.py
Detects concrete chart patterns from IndicatorSnapshot.
Each pattern returns a PatternSignal with detected=True/False, strength 0-1, and notes.
These feed into Claude's context as structured signals.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dt_time

import pandas as pd

from yukti.signals.indicators import IndicatorSnapshot


@dataclass
class PatternSignal:
    detected:     bool
    pattern_type: str
    strength:     float  # 0.0 → 1.0
    notes:        str


# ── NSE intraday session boundaries ─────────────────────────────────────────
# gap_go:         09:20–09:30 (own gate inside function)
# orb_breakout:   09:30–11:00 (own gate inside function)
# vwap_bounce:    09:45–14:40 (own gate inside function)
# breakout/breakdown:   blocked in morning rush AND dead zone
# trend_pullback: blocked in opening 30 min (no trend established)
# reversal:       valid 10:00–14:30 only
# momentum:       blocked in dead zone and EOD fade
_MORNING_RUSH_END  = dt_time(9, 30)    # before this: only gap_go, ORB
_DEAD_ZONE_START   = dt_time(10, 30)   # post-ORB quiet: false signals common
_DEAD_ZONE_END     = dt_time(11, 30)   # prime trending window resumes
_PULLBACK_START    = dt_time(9, 45)    # need at least 6 bars for trend
_REVERSAL_START    = dt_time(10, 0)    # reversals need enough history
_REVERSAL_END      = dt_time(14, 30)   # no new reversals in EOD fade
_PRIME_END         = dt_time(14, 30)   # momentum/breakout must complete before this


def _in_morning_rush(t: dt_time | None) -> bool:
    return t is not None and t < _MORNING_RUSH_END


def _in_dead_zone(t: dt_time | None) -> bool:
    return t is not None and _DEAD_ZONE_START <= t < _DEAD_ZONE_END


def _in_eod_fade(t: dt_time | None) -> bool:
    return t is not None and t >= _PRIME_END


def breakout(snap: IndicatorSnapshot, current_time: dt_time | None = None) -> PatternSignal:
    """
    Bullish breakout: price closes above recent swing high with volume surge.
    Classic setup: consolidation → expansion above resistance.
    Blocked in morning rush (ORB handles that window) and dead zone (10:30-11:30).
    """
    if _in_morning_rush(current_time) or _in_dead_zone(current_time):
        return PatternSignal(False, "breakout", 0.0, "")

    above_swing  = snap.close > snap.nearest_swing_high
    vol_surge    = snap.volume_ratio > 1.5
    bull_trend   = snap.trend == "UPTREND" or snap.supertrend_bull
    macd_bull    = snap.macd_bull
    rsi_ok       = 50 < snap.rsi < 75
    # Breakout bar must have a real body — wick-only spikes above resistance are traps
    body_ok      = snap.body_ratio > 0.35
    # Don't chase breakouts into bearish RSI divergence (price up, RSI down = exhaustion)
    no_bearish_div = not snap.rsi_bearish_divergence

    score = sum([above_swing, vol_surge, bull_trend, macd_bull, rsi_ok, body_ok, no_bearish_div])
    if score < 4 or not above_swing or not body_ok:
        return PatternSignal(False, "breakout", 0.0, "")

    strength = score / 7.0
    notes = (
        f"Price ₹{snap.close:.2f} > swing high ₹{snap.nearest_swing_high:.2f} "
        f"| body {snap.body_ratio:.0%} | vol {snap.volume_ratio:.1f}× avg"
        f"{' | MACD bull' if macd_bull else ''}"
        f"{' | RSI ' + str(round(snap.rsi, 1)) if rsi_ok else ''}"
    )
    return PatternSignal(True, "breakout", round(strength, 2), notes)


def breakdown(snap: IndicatorSnapshot, current_time: dt_time | None = None) -> PatternSignal:
    """
    Bearish breakdown: price closes below recent swing low with volume surge.
    Entry for SHORT trades.
    Blocked in morning rush and dead zone (symmetric with breakout).
    """
    if _in_morning_rush(current_time) or _in_dead_zone(current_time):
        return PatternSignal(False, "breakdown", 0.0, "")

    below_swing    = snap.close < snap.nearest_swing_low
    vol_surge      = snap.volume_ratio > 1.5
    bear_trend     = snap.trend == "DOWNTREND" or not snap.supertrend_bull
    macd_bear      = not snap.macd_bull
    rsi_ok         = 25 < snap.rsi < 50
    body_ok        = snap.body_ratio > 0.35
    no_bullish_div = not snap.rsi_bullish_divergence

    score = sum([below_swing, vol_surge, bear_trend, macd_bear, rsi_ok, body_ok, no_bullish_div])
    if score < 4 or not below_swing or not body_ok:
        return PatternSignal(False, "breakdown", 0.0, "")

    strength = score / 7.0
    notes = (
        f"Price ₹{snap.close:.2f} < swing low ₹{snap.nearest_swing_low:.2f} "
        f"| body {snap.body_ratio:.0%} | vol {snap.volume_ratio:.1f}× avg"
        f"{' | MACD bear' if macd_bear else ''}"
    )
    return PatternSignal(True, "breakdown", round(strength, 2), notes)


def trend_pullback_long(snap: IndicatorSnapshot, current_time: dt_time | None = None) -> PatternSignal:
    """
    Pullback to EMA in an uptrend: buy the dip.
    Price retreats to EMA20/EMA50, RSI cools to 40-55 zone, then bounces.
    Blocked in first 30 minutes (trend not yet established) and dead zone.
    """
    if current_time is not None and current_time < _PULLBACK_START:
        return PatternSignal(False, "trend_pullback", 0.0, "")
    if _in_dead_zone(current_time):
        return PatternSignal(False, "trend_pullback", 0.0, "")

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


def trend_pullback_short(snap: IndicatorSnapshot, current_time: dt_time | None = None) -> PatternSignal:
    """
    Rally to EMA in a downtrend: sell the bounce.
    Price rallies up to EMA20/EMA50 in a downtrend, then rolls over.
    Blocked in opening 30 min and dead zone (symmetric with long).
    """
    if current_time is not None and current_time < _PULLBACK_START:
        return PatternSignal(False, "trend_pullback_short", 0.0, "")
    if _in_dead_zone(current_time):
        return PatternSignal(False, "trend_pullback_short", 0.0, "")

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


def reversal_long(snap: IndicatorSnapshot, current_time: dt_time | None = None) -> PatternSignal:
    """
    Bullish reversal from oversold: RSI < 35, MACD turning up, at support.
    High risk / high reward — requires high conviction from Claude.
    Valid 10:00–14:30 only: too early = opening volatility, too late = EOD drift.
    Bullish divergence or hammer candle upgrades strength significantly.
    """
    if current_time is not None and (current_time < _REVERSAL_START or current_time >= _REVERSAL_END):
        return PatternSignal(False, "reversal_long", 0.0, "")

    oversold      = snap.rsi < 36
    # Histogram improving = less negative than -30% of the MACD line magnitude.
    # In oversold conditions MACD is negative; we want it "turning up", not
    # requiring it to be positive already (that would never trigger when oversold).
    macd_ref      = abs(snap.macd) if snap.macd != 0 else 1.0
    macd_turning  = snap.macd_hist > -(macd_ref * 0.3)
    at_bb_lower   = snap.close < snap.bb_lower * 1.005     # near/below BB lower
    near_swing_lo = abs(snap.close - snap.nearest_swing_low) / snap.nearest_swing_low < 0.01
    candle_green  = snap.close > snap.open
    # Hammer or bullish divergence: highest-quality reversal confirmation
    hammer_or_div = snap.is_hammer or snap.rsi_bullish_divergence

    score = sum([oversold, macd_turning, at_bb_lower, near_swing_lo, candle_green, hammer_or_div])
    if score < 3 or not oversold:
        return PatternSignal(False, "reversal_long", 0.0, "")

    strength = score / 6.0
    # Strong bonus for hammer or bullish divergence — these are high-quality signals
    if hammer_or_div:
        strength = min(strength + 0.10, 1.0)
    notes = (
        f"Oversold reversal: RSI {snap.rsi:.1f}"
        f"{' | near BB lower' if at_bb_lower else ''}"
        f"{' | at swing low ₹' + str(round(snap.nearest_swing_low, 2)) if near_swing_lo else ''}"
        f"{' | HAMMER' if snap.is_hammer else (' | bullish-div' if snap.rsi_bullish_divergence else '')}"
        f"{' | green candle' if candle_green else ''}"
    )
    return PatternSignal(True, "reversal_long", round(strength, 2), notes)


def reversal_short(snap: IndicatorSnapshot, current_time: dt_time | None = None) -> PatternSignal:
    """
    Bearish reversal from overbought: RSI > 65, at resistance, MACD rolling over.
    Valid 10:00–14:30 only (symmetric with reversal_long).
    Shooting star or bearish divergence upgrades strength.
    """
    if current_time is not None and (current_time < _REVERSAL_START or current_time >= _REVERSAL_END):
        return PatternSignal(False, "reversal_short", 0.0, "")

    overbought    = snap.rsi > 64
    at_bb_upper   = snap.close > snap.bb_upper * 0.995
    near_swing_hi = abs(snap.close - snap.nearest_swing_high) / snap.nearest_swing_high < 0.01
    candle_red    = snap.close < snap.open
    macd_hist_neg = snap.macd_hist < 0
    star_or_div   = snap.is_shooting_star or snap.rsi_bearish_divergence

    score = sum([overbought, at_bb_upper, near_swing_hi, candle_red, macd_hist_neg, star_or_div])
    if score < 3 or not overbought:
        return PatternSignal(False, "reversal_short", 0.0, "")

    strength = score / 6.0
    if star_or_div:
        strength = min(strength + 0.10, 1.0)
    notes = (
        f"Overbought reversal: RSI {snap.rsi:.1f}"
        f"{' | at BB upper' if at_bb_upper else ''}"
        f"{' | near swing high ₹' + str(round(snap.nearest_swing_high, 2)) if near_swing_hi else ''}"
        f"{' | SHOOTING STAR' if snap.is_shooting_star else (' | bearish-div' if snap.rsi_bearish_divergence else '')}"
        f"{' | red candle' if candle_red else ''}"
    )
    return PatternSignal(True, "reversal_short", round(strength, 2), notes)


def momentum_long(snap: IndicatorSnapshot, current_time: dt_time | None = None) -> PatternSignal:
    """
    Strong bullish momentum: everything aligned — trend, MACD, RSI, volume, VWAP.
    Blocked in dead zone (10:30-11:30) and EOD fade (14:30+).
    Bearish divergence is a hard block — momentum is exhausted when RSI diverges.
    """
    if _in_dead_zone(current_time) or _in_eod_fade(current_time):
        return PatternSignal(False, "momentum", 0.0, "")
    # Bearish divergence signals momentum exhaustion — do not chase
    if snap.rsi_bearish_divergence:
        return PatternSignal(False, "momentum", 0.0, "")

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


def momentum_short(snap: IndicatorSnapshot, current_time: dt_time | None = None) -> PatternSignal:
    """
    Strong bearish momentum: RSI oversold zone, MACD bearish, below VWAP/EMA20, volume surge.
    Intraday only — swing shorts are blocked at the risk layer per NSE rules.
    Blocked in dead zone and EOD fade (symmetric with momentum_long).
    Bullish divergence is a hard block — selling momentum is exhausted.
    """
    if _in_dead_zone(current_time) or _in_eod_fade(current_time):
        return PatternSignal(False, "momentum_short", 0.0, "")
    if snap.rsi_bullish_divergence:
        return PatternSignal(False, "momentum_short", 0.0, "")

    rsi_momentum  = 28 <= snap.rsi <= 42
    macd_bear     = not snap.macd_bull and snap.macd_hist < 0
    below_vwap    = not snap.above_vwap()
    below_ema20   = not snap.above_ema20()
    vol_surge     = snap.volume_ratio > 1.3
    supertrend    = not snap.supertrend_bull

    score = sum([rsi_momentum, macd_bear, below_vwap, below_ema20, vol_surge, supertrend])
    if score < 4:
        return PatternSignal(False, "momentum_short", 0.0, "")

    strength = score / 6.0
    notes = (
        f"Momentum Short: RSI {snap.rsi:.1f}"
        f" | MACD hist {snap.macd_hist:+.3f}"
        f" | vol {snap.volume_ratio:.1f}×"
        f" | {'below' if below_ema20 else 'above'} EMA20"
    )
    return PatternSignal(True, "momentum_short", round(strength, 2), notes)


def orb_breakout(
    snap: IndicatorSnapshot,
    candles: pd.DataFrame | None = None,
    current_time: dt_time | None = None,
    indicators_daily: IndicatorSnapshot | None = None,
) -> PatternSignal:
    """
    Opening Range Breakout: first 15 min (3 × 5-min candles) define the range.
    Breakout above OR_High → LONG. Breakdown below OR_Low → SHORT.
    Valid 09:30–11:00 only.
    """
    # Time gate
    if current_time is None or current_time < dt_time(9, 30) or current_time > dt_time(11, 0):
        return PatternSignal(False, "orb_breakout", 0.0, "")

    # Need at least 3 candles for opening range
    if candles is None or len(candles) < 3:
        return PatternSignal(False, "orb_breakout", 0.0, "")

    # Compute opening range from first 3 fully-closed candles (avoid look-ahead
    # when callers pass a live/unclosed current candle). Ensure ascending order.
    candles_sorted = candles.sort_index() if hasattr(candles, "sort_index") else candles
    closed_candles = candles_sorted
    if current_time is not None:
        try:
            idx_times = pd.DatetimeIndex(candles_sorted.index).time
            mask = [t < current_time for t in idx_times]
            closed_candles = candles_sorted.loc[mask]
        except Exception:
            closed_candles = candles_sorted

    if len(closed_candles) < 3:
        return PatternSignal(False, "orb_breakout", 0.0, "")

    or_candles = closed_candles.iloc[:3]
    or_high = float(or_candles["high"].max())
    or_low = float(or_candles["low"].min())
    or_mid = (or_high + or_low) / 2

    # Gap filter: large opening gaps (>1.5%) signal gap-and-reverse days where
    # the OR is faked out — price breaks out then immediately reverses into the gap.
    # Use yesterday's daily close (indicators_daily.prev_close) vs today's first open.
    if indicators_daily is not None and indicators_daily.prev_close > 0:
        today_open = float(or_candles.iloc[0]["open"])
        gap_pct = abs(today_open - indicators_daily.prev_close) / indicators_daily.prev_close
        if gap_pct > 0.015:
            return PatternSignal(False, "orb_breakout", 0.0, "")

    # Determine direction
    long_break = snap.close > or_high
    short_break = snap.close < or_low

    if not long_break and not short_break:
        return PatternSignal(False, "orb_breakout", 0.0, "")

    # Daily trend filter (skip if daily data unavailable)
    daily_trend = indicators_daily.trend if indicators_daily else "SIDEWAYS"

    if long_break:
        vol_ok = snap.volume_ratio >= 1.5
        rsi_ok = 45 <= snap.rsi <= 70   # widened from 50–70: catches early breakouts
        daily_ok = daily_trend != "DOWNTREND"
        if not vol_ok or not rsi_ok or not daily_ok:
            return PatternSignal(False, "orb_breakout", 0.0, "")

        strength = 0.5
        if snap.volume_ratio > 2.0:
            strength += 0.15
        if daily_trend == "UPTREND":
            strength += 0.15
        or_range = or_high - or_low
        if snap.atr > 0 and or_range < snap.atr:
            strength += 0.10
        if snap.prev_close <= or_high * 1.002:
            strength += 0.10

        notes = (
            f"ORB LONG: close ₹{snap.close:.2f} > OR_High ₹{or_high:.2f} "
            f"| range ₹{or_range:.2f} | vol {snap.volume_ratio:.1f}×"
            f"{' | daily aligned' if daily_trend == 'UPTREND' else ''}"
        )
        return PatternSignal(True, "orb_breakout_long", round(min(strength, 1.0), 2), notes)

    if short_break:
        vol_ok = snap.volume_ratio >= 1.5
        rsi_ok = 30 <= snap.rsi <= 55   # widened from 30–50: covers post-breakdown momentum
        daily_ok = daily_trend != "UPTREND"
        if not vol_ok or not rsi_ok or not daily_ok:
            return PatternSignal(False, "orb_breakout", 0.0, "")

        strength = 0.5
        if snap.volume_ratio > 2.0:
            strength += 0.15
        if daily_trend == "DOWNTREND":
            strength += 0.15
        or_range = or_high - or_low
        if snap.atr > 0 and or_range < snap.atr:
            strength += 0.10
        if snap.prev_close >= or_low * 0.998:
            strength += 0.10

        notes = (
            f"ORB SHORT: close ₹{snap.close:.2f} < OR_Low ₹{or_low:.2f} "
            f"| range ₹{or_range:.2f} | vol {snap.volume_ratio:.1f}×"
            f"{' | daily aligned' if daily_trend == 'DOWNTREND' else ''}"
        )
        return PatternSignal(True, "orb_breakout_short", round(min(strength, 1.0), 2), notes)

    return PatternSignal(False, "orb_breakout", 0.0, "")


def vwap_bounce(
    snap: IndicatorSnapshot,
    candles: pd.DataFrame | None = None,
    current_time: dt_time | None = None,
    indicators_daily: IndicatorSnapshot | None = None,
) -> PatternSignal:
    """
    VWAP Bounce: price touches/crosses VWAP then bounces back.
    Institutional magnet — high probability in trending stocks.
    Valid 09:45–14:40 only.
    """
    # Time gate
    if current_time is None or current_time < dt_time(9, 45) or current_time > dt_time(14, 40):
        return PatternSignal(False, "vwap_bounce", 0.0, "")

    if candles is None or len(candles) < 3:
        return PatternSignal(False, "vwap_bounce", 0.0, "")

    vwap = snap.vwap
    daily_trend = indicators_daily.trend if indicators_daily else "SIDEWAYS"

    # Check if price touched VWAP in recent candles (last 2-3)
    recent = candles.iloc[-3:] if len(candles) >= 3 else candles
    recent_lows = recent["low"].values
    recent_highs = recent["high"].values

    touched_below = any(float(lo) <= vwap for lo in recent_lows)
    touched_above = any(float(hi) >= vwap for hi in recent_highs)

    # VWAP Bounce Long
    if snap.close > vwap and touched_below:
        uptrend = snap.ema20 > snap.ema50
        rsi_ok = 40 <= snap.rsi <= 60
        vol_ok = snap.volume_ratio > 1.0
        macd_improving = snap.macd_hist > 0 or snap.macd_bull

        # Hard daily-trend filter: don't fade an established downtrend
        if daily_trend == "DOWNTREND":
            return PatternSignal(False, "vwap_bounce", 0.0, "")

        if uptrend and rsi_ok and vol_ok and macd_improving:
            strength = 0.5
            if daily_trend == "UPTREND":
                strength += 0.15
            if snap.supertrend_bull:
                strength += 0.15
            # Clean bounce: wick touched VWAP, body stayed above
            bounce_candle = candles.iloc[-1]
            if float(bounce_candle["low"]) <= vwap <= float(bounce_candle["open"]):
                strength += 0.10
            if snap.close < snap.bb_mid:
                strength += 0.10

            notes = (
                f"VWAP Bounce LONG: close ₹{snap.close:.2f} > VWAP ₹{vwap:.2f} "
                f"| RSI {snap.rsi:.1f} | vol {snap.volume_ratio:.1f}×"
                f"{' | daily aligned' if daily_trend == 'UPTREND' else ''}"
            )
            return PatternSignal(True, "vwap_bounce_long", round(min(strength, 1.0), 2), notes)

    # VWAP Bounce Short (Rejection)
    if snap.close < vwap and touched_above:
        downtrend = snap.ema20 < snap.ema50
        rsi_ok = 40 <= snap.rsi <= 60
        vol_ok = snap.volume_ratio > 1.0
        macd_declining = snap.macd_hist < 0 or not snap.macd_bull

        # Hard daily-trend filter: don't fade an established uptrend
        if daily_trend == "UPTREND":
            return PatternSignal(False, "vwap_bounce", 0.0, "")

        if downtrend and rsi_ok and vol_ok and macd_declining:
            strength = 0.5
            if daily_trend == "DOWNTREND":
                strength += 0.15
            if not snap.supertrend_bull:
                strength += 0.15
            bounce_candle = candles.iloc[-1]
            if float(bounce_candle["high"]) >= vwap >= float(bounce_candle["open"]):
                strength += 0.10
            if snap.close > snap.bb_mid:
                strength += 0.10

            notes = (
                f"VWAP Bounce SHORT (rejection): close ₹{snap.close:.2f} < VWAP ₹{vwap:.2f} "
                f"| RSI {snap.rsi:.1f} | vol {snap.volume_ratio:.1f}×"
                f"{' | daily aligned' if daily_trend == 'DOWNTREND' else ''}"
            )
            return PatternSignal(True, "vwap_bounce_short", round(min(strength, 1.0), 2), notes)

    return PatternSignal(False, "vwap_bounce", 0.0, "")


def gap_go(
    snap: IndicatorSnapshot,
    candles: pd.DataFrame | None = None,
    current_time: dt_time | None = None,
    indicators_daily: IndicatorSnapshot | None = None,
) -> PatternSignal:
    """
    Gap & Go: trade the continuation of an opening gap in the first 15 minutes.

    After the first 5m candle closes (09:20), if the stock gapped 0.5–2% and the
    first candle confirmed the gap direction (closes in upper/lower half with high
    volume), enter on the breakout of that first candle's high/low.

    Gap < 0.5%: not a real gap — random overnight drift, no momentum edge.
    Gap > 2%: risk of gap-fill reversal; these are better traded as reversals.
    Valid 09:20–09:30 only — after that ORB takes over.
    """
    if current_time is None or current_time < dt_time(9, 20) or current_time >= dt_time(9, 30):
        return PatternSignal(False, "gap_go", 0.0, "")

    if candles is None or len(candles) < 1:
        return PatternSignal(False, "gap_go", 0.0, "")

    if indicators_daily is None or indicators_daily.prev_close <= 0:
        return PatternSignal(False, "gap_go", 0.0, "")

    prev_close   = indicators_daily.prev_close
    first        = candles.iloc[0]   # the 09:15–09:20 candle
    today_open   = float(first["open"])
    first_high   = float(first["high"])
    first_low    = float(first["low"])
    first_open   = float(first["open"])
    first_close  = float(first["close"])
    candle_range = first_high - first_low

    gap_pct = (today_open - prev_close) / prev_close   # + = gap up, - = gap down
    gap_abs = abs(gap_pct)

    # Gap must be 0.5–2%
    if gap_abs < 0.005 or gap_abs > 0.02:
        return PatternSignal(False, "gap_go", 0.0, "")

    daily_trend = indicators_daily.trend if indicators_daily else "SIDEWAYS"

    if gap_pct > 0:   # ── Gap Up → Long ──────────────────────────────────────
        candle_bullish = first_close > first_open
        # Close in upper 40% of the candle range (momentum confirmation)
        closes_high    = candle_range > 0 and (first_close - first_low) / candle_range >= 0.60
        vol_ok         = snap.volume_ratio >= 2.5
        rsi_ok         = snap.rsi < 72
        daily_ok       = daily_trend != "DOWNTREND"
        # Price must have broken above first candle high (entry trigger)
        breaking_out   = snap.close >= first_high

        if not (candle_bullish and closes_high and vol_ok and rsi_ok and daily_ok and breaking_out):
            return PatternSignal(False, "gap_go", 0.0, "")

        strength = 0.60
        if daily_trend == "UPTREND":  strength += 0.15
        if snap.volume_ratio >= 3.5:  strength += 0.10
        if gap_abs >= 0.01:           strength += 0.10   # 1%+ gap = stronger momentum
        if snap.macd_bull:            strength += 0.05

        notes = (
            f"Gap & Go LONG: gap +{gap_pct*100:.1f}% (open ₹{today_open:.2f} vs prev ₹{prev_close:.2f}) "
            f"| first candle breaks ₹{first_high:.2f} | vol {snap.volume_ratio:.1f}×"
            f"{' | daily aligned' if daily_trend == 'UPTREND' else ''}"
        )
        return PatternSignal(True, "gap_go_long", round(min(strength, 1.0), 2), notes)

    else:   # ── Gap Down → Short ────────────────────────────────────────────
        candle_bearish = first_close < first_open
        # Close in lower 40% of the candle range
        closes_low     = candle_range > 0 and (first_high - first_close) / candle_range >= 0.60
        vol_ok         = snap.volume_ratio >= 2.5
        rsi_ok         = snap.rsi > 28
        daily_ok       = daily_trend != "UPTREND"
        breaking_down  = snap.close <= first_low

        if not (candle_bearish and closes_low and vol_ok and rsi_ok and daily_ok and breaking_down):
            return PatternSignal(False, "gap_go", 0.0, "")

        strength = 0.60
        if daily_trend == "DOWNTREND": strength += 0.15
        if snap.volume_ratio >= 3.5:   strength += 0.10
        if gap_abs >= 0.01:            strength += 0.10
        if not snap.macd_bull:         strength += 0.05

        notes = (
            f"Gap & Go SHORT: gap {gap_pct*100:.1f}% (open ₹{today_open:.2f} vs prev ₹{prev_close:.2f}) "
            f"| first candle breaks ₹{first_low:.2f} | vol {snap.volume_ratio:.1f}×"
            f"{' | daily aligned' if daily_trend == 'DOWNTREND' else ''}"
        )
        return PatternSignal(True, "gap_go_short", round(min(strength, 1.0), 2), notes)


def scan_all(
    snap: IndicatorSnapshot,
    candles: pd.DataFrame | None = None,
    indicators_daily: IndicatorSnapshot | None = None,
    current_time: dt_time | None = None,
) -> list[PatternSignal]:
    """
    Run all pattern detectors and return detected signals sorted by strength.
    """
    all_patterns = [
        breakout(snap, current_time),
        breakdown(snap, current_time),
        trend_pullback_long(snap, current_time),
        trend_pullback_short(snap, current_time),
        reversal_long(snap, current_time),
        reversal_short(snap, current_time),
        momentum_long(snap, current_time),
        momentum_short(snap, current_time),
        orb_breakout(snap, candles, current_time, indicators_daily),
        vwap_bounce(snap, candles, current_time, indicators_daily),
        gap_go(snap, candles, current_time, indicators_daily),
    ]
    detected = [p for p in all_patterns if p.detected]
    return sorted(detected, key=lambda p: p.strength, reverse=True)


def best_pattern(
    snap: IndicatorSnapshot,
    candles: pd.DataFrame | None = None,
    indicators_daily: IndicatorSnapshot | None = None,
    current_time: dt_time | None = None,
) -> PatternSignal | None:
    """Return the single highest-strength detected pattern, or None."""
    found = scan_all(snap, candles, indicators_daily, current_time)
    return found[0] if found else None
