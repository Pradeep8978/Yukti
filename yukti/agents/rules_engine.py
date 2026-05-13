"""
yukti/agents/rules_engine.py
Deterministic rule-based trading decisions — no AI API calls.

Implements the same decision framework as Arjun's system prompt but as
explicit, auditable rules. Enabled when settings.use_ai_decision=False.

Output is identical in type (TradeDecision) so all downstream code
(execution, logging, DecisionLog) works unchanged.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from yukti.agents.arjun import CallMeta, TradeDecision

log = logging.getLogger(__name__)

# Pattern types that imply a LONG direction
_LONG_PATTERNS  = frozenset({"breakout", "trend_pullback", "reversal_long", "momentum", "orb_breakout"})
# Pattern types that imply a SHORT direction
_SHORT_PATTERNS = frozenset({"breakdown", "trend_pullback_short", "reversal_short", "momentum_short"})


@dataclass
class _Score:
    """Mutable conviction score with a human-readable audit trail."""
    value: int = 6  # neutral baseline
    notes: list[str] = None

    def __post_init__(self) -> None:
        self.notes = []

    def adjust(self, delta: int, reason: str) -> None:
        self.value += delta
        sign = "+" if delta >= 0 else ""
        self.notes.append(f"{sign}{delta} {reason}")

    def clamp(self) -> int:
        self.value = max(1, min(10, self.value))
        return self.value

    def summary(self) -> str:
        return f"base=6 → {' → '.join(self.notes)} = {self.value}"


def _skip(reason: str, conviction: int = 1, bias: str = "NEUTRAL", symbol: str = "UNKNOWN") -> TradeDecision:
    return TradeDecision(
        symbol=symbol,
        action="SKIP",
        market_bias=bias,
        reasoning=f"Rules engine: {reason}",
        conviction=conviction,
        skip_reason=reason,
    )


def decide(
    symbol: str,
    snap,           # IndicatorSnapshot
    macro,          # MacroContext
    perf: dict,
    pattern,        # PatternSignal | None
    snap_daily=None,  # IndicatorSnapshot | None
) -> tuple[TradeDecision, CallMeta]:
    """
    Deterministic trade decision. Returns (TradeDecision, CallMeta).
    Always succeeds — never raises.
    """
    t0 = time.monotonic()

    try:
        decision = _decide_inner(symbol, snap, macro, perf, pattern, snap_daily)
    except Exception as exc:
        log.error("rules_engine.decide crashed for %s: %s", symbol, exc, exc_info=True)
        decision = _skip("rules_engine_error", symbol=symbol)

    latency_ms = (time.monotonic() - t0) * 1000
    meta = CallMeta(
        provider="rules",
        model="rules_engine_v1",
        latency_ms=latency_ms,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
    )
    log.info(
        "[RULES] %s %s conviction=%d bias=%s  %.0fms",
        decision.action,
        decision.direction or "—",
        decision.conviction,
        decision.market_bias,
        latency_ms,
    )
    return decision, meta


def _decide_inner(symbol, snap, macro, perf, pattern, snap_daily) -> TradeDecision:
    # ── Step 0: Performance guards ─────────────────────────────────────
    daily_pnl    = perf.get("daily_pnl_pct", 0.0)
    consec_losses = perf.get("consecutive_losses", 0)
    win_rate     = perf.get("win_rate_last_10", 1.0)

    if daily_pnl <= -2.0:
        return _skip("daily_loss_limit_hit", conviction=1, symbol=symbol)

    # Minimum conviction gate (tightens under adverse conditions)
    min_conviction = 7
    if consec_losses >= 3:
        min_conviction = 9
    elif win_rate < 0.40:
        min_conviction = 9
    elif daily_pnl >= 3.0:
        min_conviction = 8

    # ── Step 1: Pattern required ───────────────────────────────────────
    if pattern is None or not pattern.detected:
        return _skip("no_pattern", symbol=symbol)

    if pattern.pattern_type in _LONG_PATTERNS:
        direction = "LONG"
    elif pattern.pattern_type in _SHORT_PATTERNS:
        direction = "SHORT"
    else:
        return _skip(f"unknown_pattern_type:{pattern.pattern_type}", symbol=symbol)

    # ── Step 2: Market bias from Nifty ────────────────────────────────
    nifty_trend = macro.nifty_trend   # "UP" | "DOWN" | "SIDEWAYS"
    nifty_chg   = macro.nifty_chg_pct

    if nifty_trend == "UP" and nifty_chg > 0.5:
        market_bias = "BULLISH"
    elif nifty_trend == "DOWN" and nifty_chg < -0.5:
        market_bias = "BEARISH"
    else:
        market_bias = "NEUTRAL"

    # ── Step 3: Hard gates (VIX + RSI) ────────────────────────────────
    india_vix = getattr(macro, "india_vix", None)
    if india_vix is not None and india_vix >= 30:
        # Extreme volatility — rules engine cannot price risk reliably at this level
        return _skip("vix_extreme", conviction=1, bias=market_bias, symbol=symbol)

    # RSI oversold (<30) blocks new shorts; RSI overbought (>70) blocks new longs.
    if direction == "SHORT" and snap.rsi_oversold():
        return _skip("rsi_oversold_blocks_short", conviction=2, bias=market_bias, symbol=symbol)
    if direction == "LONG" and snap.rsi_overbought():
        return _skip("rsi_overbought_blocks_long", conviction=2, bias=market_bias, symbol=symbol)

    # ── Step 4: Conviction scoring ─────────────────────────────────────
    score = _Score()

    # Nifty alignment
    if direction == "LONG":
        if market_bias == "BULLISH":
            score.adjust(+1, "nifty_aligned_long")
        elif market_bias == "BEARISH":
            score.adjust(-2, "nifty_counter_long")
    else:  # SHORT
        if market_bias == "BEARISH":
            score.adjust(+1, "nifty_aligned_short")
        elif market_bias == "BULLISH":
            score.adjust(-2, "nifty_counter_short")

    # PCR alignment (use raw float, avoids string parsing)
    pcr = getattr(macro, "nifty_pcr", None)
    if pcr is not None:
        if pcr > 1.3 and direction == "SHORT":
            score.adjust(+1, "pcr_heavy_put_short")
        elif pcr > 1.3 and direction == "LONG":
            score.adjust(-1, "pcr_heavy_put_long")
        elif pcr < 0.7 and direction == "LONG":
            score.adjust(+1, "pcr_heavy_call_long")
        elif pcr < 0.7 and direction == "SHORT":
            score.adjust(-1, "pcr_heavy_call_short")

    # Pattern strength
    if pattern.strength >= 0.85:
        score.adjust(+1, "pattern_strong")
    elif pattern.strength < 0.60:
        score.adjust(-1, "pattern_weak")

    # Volume
    if snap.volume_ratio >= 1.5:
        score.adjust(+1, "vol_high")
    elif snap.volume_ratio < 0.8:
        score.adjust(-1, "vol_low")

    # Supertrend alignment
    if direction == "LONG" and snap.supertrend_bull:
        score.adjust(+1, "supertrend_bull")
    elif direction == "SHORT" and not snap.supertrend_bull:
        score.adjust(+1, "supertrend_bear")
    else:
        score.adjust(-1, "supertrend_misaligned")

    # VWAP position — institutions use VWAP as reference; misalignment is a real headwind
    if direction == "LONG":
        if snap.above_vwap():
            score.adjust(+1, "above_vwap")
        else:
            score.adjust(-1, "below_vwap_long")
    else:
        if not snap.above_vwap():
            score.adjust(+1, "below_vwap")
        else:
            score.adjust(-1, "above_vwap_short")

    # MACD direction — momentum confirmation bonus (no penalty: pattern already carries this)
    if direction == "LONG" and snap.macd_bull:
        score.adjust(+1, "macd_bull")
    elif direction == "SHORT" and not snap.macd_bull:
        score.adjust(+1, "macd_bear")

    # EMA20 position — price above/below short-term trend
    if direction == "LONG" and snap.above_ema20():
        score.adjust(+1, "above_ema20")
    elif direction == "SHORT" and not snap.above_ema20():
        score.adjust(+1, "below_ema20")

    # Elevated VIX — markets are nervous, reduce conviction (not a hard stop below 30)
    if india_vix is not None and india_vix >= 20:
        score.adjust(-1, f"vix_elevated_{india_vix:.0f}")

    # ── Step 5: Daily timeframe (multi-timeframe alignment) ───────────
    if snap_daily is not None:
        daily_trend = snap_daily.trend  # "UPTREND" | "DOWNTREND" | "SIDEWAYS"

        if direction == "LONG" and daily_trend == "UPTREND":
            score.adjust(+1, "daily_aligned_long")
        elif direction == "SHORT" and daily_trend == "DOWNTREND":
            score.adjust(+1, "daily_aligned_short")
        elif direction == "LONG" and daily_trend == "DOWNTREND":
            score.adjust(-2, "daily_counter_long")
        elif direction == "SHORT" and daily_trend == "UPTREND":
            score.adjust(-2, "daily_counter_short")

        # Daily RSI extremes reduce conviction (stretched moves)
        if snap_daily.rsi > 75:
            score.adjust(-1, "daily_rsi_overbought")
        elif snap_daily.rsi < 25:
            score.adjust(-1, "daily_rsi_oversold")

        # Don't short at daily support / don't long at daily resistance
        if direction == "SHORT" and snap_daily.daily_support:
            dist_pct = (snap.close - snap_daily.daily_support) / snap.close
            if dist_pct < 0.005:
                return _skip("price_at_daily_support", conviction=score.clamp(), bias=market_bias, symbol=symbol)

        if direction == "LONG" and snap_daily.daily_resistance:
            dist_pct = (snap_daily.daily_resistance - snap.close) / snap.close
            if dist_pct < 0.005:
                return _skip("price_at_daily_resistance", conviction=score.clamp(), bias=market_bias, symbol=symbol)

    conviction = score.clamp()
    log.debug("Rules conviction for %s (%s %s): %s", symbol, direction, pattern.pattern_type, score.summary())

    # ── Step 6: Minimum conviction gate ───────────────────────────────
    # Counter-trend against a confirmed daily trend always needs conviction ≥ 9.
    # This mirrors the system prompt: "only trade WITH the trend unless conviction ≥ 9."
    # ADX unknown → assume trend is significant (fail-safe towards skip).
    if snap_daily is not None:
        _daily_counter = (
            (direction == "LONG"  and snap_daily.trend == "DOWNTREND") or
            (direction == "SHORT" and snap_daily.trend == "UPTREND")
        )
        if _daily_counter:
            adx = getattr(snap_daily, "adx", None)
            if adx is None or adx > 20:
                min_conviction = max(min_conviction, 8)

    if conviction < min_conviction:
        return _skip(
            f"conviction_{conviction}_below_min_{min_conviction}",
            conviction=conviction,
            bias=market_bias,
            symbol=symbol,
        )

    # ── Step 7: Calculate levels ───────────────────────────────────────
    entry = snap.close
    atr   = snap.atr

    # Widen SL multiplier when options market shows elevated IV (system prompt rule)
    atm_iv = getattr(macro, "nifty_atm_iv", None)
    sl_mult = 2.0 if (atm_iv is not None and atm_iv > 25) else 1.5

    if direction == "LONG":
        sl         = max(entry - sl_mult * atr, snap.nearest_swing_low * 0.995)
        stop_dist  = entry - sl
    else:
        sl         = min(entry + sl_mult * atr, snap.nearest_swing_high * 1.005)
        stop_dist  = sl - entry

    # Reject wide stops — signals a poor entry point
    if stop_dist > 2.5 * atr:
        return _skip("stop_too_wide", conviction=conviction, bias=market_bias, symbol=symbol)

    # Avoid division-by-zero on degenerate ATR
    if stop_dist <= 0:
        return _skip("zero_stop_distance", conviction=conviction, bias=market_bias, symbol=symbol)

    if direction == "LONG":
        t1 = entry + 2.0 * stop_dist
        t2 = entry + 3.0 * stop_dist
    else:
        t1 = entry - 2.0 * stop_dist
        t2 = entry - 3.0 * stop_dist

    vwap_side = "above" if snap.above_vwap() else "below"
    iv_note   = f" IV={atm_iv:.0f}% sl×{sl_mult}" if atm_iv else ""
    reasoning = (
        f"Rules engine: {pattern.pattern_type} on {symbol}. "
        f"Nifty {market_bias} ({nifty_chg:+.2f}%, trend={nifty_trend}). "
        f"RSI {snap.rsi:.1f}, MACD {'bull' if snap.macd_bull else 'bear'}, "
        f"VWAP {vwap_side}, vol {snap.volume_ratio:.1f}×, "
        f"Supertrend {'bull' if snap.supertrend_bull else 'bear'}, "
        f"ATR ₹{atr:.2f}{iv_note}. Conviction {conviction}/10. "
        f"Score: {score.summary()}."
    )

    return TradeDecision(
        symbol=symbol,
        action="TRADE",
        direction=direction,
        market_bias=market_bias,
        setup_type=pattern.pattern_type,
        reasoning=reasoning,
        entry_price=round(entry, 2),
        entry_type="LIMIT",
        stop_loss=round(sl, 2),
        target_1=round(t1, 2),
        target_2=round(t2, 2),
        conviction=conviction,
        risk_reward=2.0,
        holding_period="intraday",
    )
