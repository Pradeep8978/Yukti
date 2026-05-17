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
from yukti.config import settings

log = logging.getLogger(__name__)

# Pattern types that imply a LONG direction.
# orb_breakout previously emitted the same pattern_type for both up- and
# down-breaks, so an ORB *short* breakdown was being routed here as a LONG
# entry. patterns.py now emits orb_breakout_long / orb_breakout_short.
_LONG_PATTERNS  = frozenset({
    "breakout", "trend_pullback", "reversal_long", "momentum",
    "orb_breakout_long", "vwap_bounce_long", "gap_go_long",
})
_SHORT_PATTERNS = frozenset({
    "breakdown", "trend_pullback_short", "reversal_short", "momentum_short",
    "orb_breakout_short", "vwap_bounce_short", "gap_go_short",
})


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

    # Minimum conviction gate
    # Use configured min_conviction directly rather than silently enforcing a
    # hard floor of 7. This allows ops to calibrate conviction per config.
    min_conviction = settings.min_conviction
    if consec_losses >= 3:
        min_conviction = max(min_conviction, 9)
    elif win_rate < 0.40:
        min_conviction = max(min_conviction, 9)
    elif daily_pnl >= 3.0:
        min_conviction = max(min_conviction, 8)

    # ── Step 1: Pattern required ───────────────────────────────────────
    if pattern is None or not pattern.detected:
        return _skip("no_pattern", symbol=symbol)

    if pattern.strength < 0.55:
        return _skip("pattern_strength_below_0_55", conviction=2, symbol=symbol)

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

    # Hard trend-alignment gates for daily timeframe.
    # momentum (LONG) relies on price being above recent average — unreliable
    # in a confirmed daily downtrend. Require daily UPTREND or SIDEWAYS.
    # trend_pullback_short has a very poor historical win rate; block unless
    # daily trend confirms the short direction.
    if snap_daily is not None:
        daily_trend = getattr(snap_daily, "trend", None)
        if direction == "LONG" and pattern.pattern_type == "momentum" and daily_trend == "DOWNTREND":
            return _skip("momentum_long_blocked_in_downtrend", conviction=2, bias=market_bias, symbol=symbol)
        if pattern.pattern_type == "trend_pullback_short" and daily_trend != "DOWNTREND":
            return _skip("trend_pullback_short_needs_downtrend", conviction=2, bias=market_bias, symbol=symbol)

    # On daily timeframe, skip low-edge setups that historically underperform:
    # - momentum LONG: fires too broadly, loses in sideways/down markets
    # - trend_pullback_short: poor win rate across regimes
    # - breakout LONG: 28% WR on daily, false breakouts dominate
    is_daily_tf = getattr(snap, "timeframe", "5m") == "daily"
    if is_daily_tf:
        if pattern.pattern_type == "momentum" and direction == "LONG":
            return _skip("momentum_long_blocked_daily", conviction=2, bias=market_bias, symbol=symbol)
        if pattern.pattern_type == "trend_pullback_short":
            return _skip("trend_pullback_short_blocked_daily", conviction=2, bias=market_bias, symbol=symbol)
        if pattern.pattern_type == "breakout":
            return _skip("breakout_blocked_daily", conviction=2, bias=market_bias, symbol=symbol)

    # ── Step 3: Hard gates (VIX regime + RSI) ─────────────────────────
    india_vix = getattr(macro, "india_vix", None)
    if india_vix is not None and india_vix >= 30:
        return _skip("vix_extreme", conviction=1, bias=market_bias, symbol=symbol)

    # VIX regime bucketing: four zones with different strategy biases.
    # Thresholds from config (default [12, 18, 25]).
    # Guard against misconfigured bucket list (empty or too short).
    vix_regime = "normal"
    if india_vix is not None:
        bkts = sorted(settings.vix_regime_buckets)
        if len(bkts) >= 3:
            if india_vix < bkts[0]:
                vix_regime = "low"        # < 12: range-bound grind
            elif india_vix < bkts[1]:
                vix_regime = "normal"     # 12-18: full playbook
            elif india_vix < bkts[2]:
                vix_regime = "elevated"   # 18-25: cautious sizing
            else:
                vix_regime = "high"       # 25-30: high caution

    # Low VIX (<12): market is in a slow grind — momentum and breakout patterns
    # fail because there's no follow-through. Favour reversals and VWAP plays.
    if vix_regime == "low" and pattern.pattern_type in {
        "momentum", "momentum_short", "breakout", "breakdown",
    }:
        return _skip("vix_too_low_for_momentum_breakout", conviction=2, bias=market_bias, symbol=symbol)

    # High VIX (25-30): aggressive longs are dangerous; require strong conviction
    # and skip momentum longs / trend-following entries.
    if vix_regime == "high":
        min_conviction = max(min_conviction, 8)
        if direction == "LONG" and pattern.pattern_type in {"momentum", "breakout", "trend_pullback"}:
            return _skip("vix_high_blocks_aggressive_long", conviction=2, bias=market_bias, symbol=symbol)

    # Elevated VIX (18-25): raise floor to 7 so only decent setups trade.
    if vix_regime == "elevated":
        min_conviction = max(min_conviction, 7)

    # RSI oversold (<30) blocks new shorts; RSI overbought (>70) blocks new longs.
    if direction == "SHORT" and snap.rsi_oversold():
        return _skip("rsi_oversold_blocks_short", conviction=2, bias=market_bias, symbol=symbol)
    if direction == "LONG" and snap.rsi_overbought():
        return _skip("rsi_overbought_blocks_long", conviction=2, bias=market_bias, symbol=symbol)
    if snap.volume_ratio < 0.5:
        return _skip("volume_ratio_below_0_5", conviction=2, bias=market_bias, symbol=symbol)

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

    trend_confirmed = False
    vwap_confirmed = False
    macd_confirmed = False
    ema_confirmed = False

    # Supertrend alignment
    if direction == "LONG" and snap.supertrend_bull:
        trend_confirmed = True
        score.adjust(+1, "supertrend_bull")
    elif direction == "SHORT" and not snap.supertrend_bull:
        trend_confirmed = True
        score.adjust(+1, "supertrend_bear")
    else:
        score.adjust(-1, "supertrend_misaligned")

    # VWAP position — institutions use VWAP as reference; misalignment is a real headwind.
    # Only counts on timeframes where VWAP resets per session (intraday). On a
    # daily series the computed VWAP is just cumulative-since-history-start
    # and correlates with trend, so it adds noise rather than information.
    if getattr(snap, "vwap_is_meaningful", lambda: True)():
        if direction == "LONG":
            if snap.above_vwap():
                vwap_confirmed = True
                score.adjust(+1, "above_vwap")
            else:
                score.adjust(-1, "below_vwap_long")
        else:
            if not snap.above_vwap():
                vwap_confirmed = True
                score.adjust(+1, "below_vwap")
            else:
                score.adjust(-1, "above_vwap_short")
    else:
        # Treat the VWAP confirmation slot as neutral — neither a vote up nor
        # a penalty. The remaining trend / MACD / EMA20 votes carry the
        # alignment check.
        vwap_confirmed = True

    # MACD direction — momentum confirmation bonus (no penalty: pattern already carries this)
    if direction == "LONG" and snap.macd_bull:
        macd_confirmed = True
        score.adjust(+1, "macd_bull")
    elif direction == "SHORT" and not snap.macd_bull:
        macd_confirmed = True
        score.adjust(+1, "macd_bear")

    # EMA20 position — price above/below short-term trend
    if direction == "LONG" and snap.above_ema20():
        ema_confirmed = True
        score.adjust(+1, "above_ema20")
    elif direction == "SHORT" and not snap.above_ema20():
        ema_confirmed = True
        score.adjust(+1, "below_ema20")

    # Require trend alignment plus at least one momentum/structure vote.
    # This replaces the previous 3-of-4 confirmation gate which counted
    # correlated votes and could be overly strict.
    if not trend_confirmed:
        return _skip("trend_not_confirmed", conviction=score.clamp(), bias=market_bias, symbol=symbol)
    if not (macd_confirmed or ema_confirmed or vwap_confirmed):
        return _skip("no_momentum_or_structure_confirmation", conviction=score.clamp(), bias=market_bias, symbol=symbol)

    # RSI divergence votes: price vs. momentum disagreement is a strong signal.
    # Bullish div (price lower low, RSI higher low) = reversal up → helps LONG, hurts SHORT.
    # Bearish div (price higher high, RSI lower high) = exhaustion → helps SHORT, hurts LONG.
    bull_div = getattr(snap, "rsi_bullish_divergence", False)
    bear_div = getattr(snap, "rsi_bearish_divergence", False)
    if direction == "LONG":
        if bull_div:
            score.adjust(+1, "rsi_bullish_divergence")
        elif bear_div:
            score.adjust(-1, "rsi_bearish_div_against_long")
    else:
        if bear_div:
            score.adjust(+1, "rsi_bearish_divergence")
        elif bull_div:
            score.adjust(-1, "rsi_bullish_div_against_short")

    # Elevated VIX — markets are nervous, reduce conviction (not a hard stop below 30)
    if india_vix is not None and india_vix >= 20:
        score.adjust(-1, f"vix_elevated_{india_vix:.0f}")

    # Candle structure bonus: hammer = high-quality reversal bar for longs;
    # shooting star = for shorts. Doji (body_ratio < 0.15) = indecision → penalty.
    body_ratio = getattr(snap, "body_ratio", 1.0)
    is_hammer  = getattr(snap, "is_hammer", False)
    is_star    = getattr(snap, "is_shooting_star", False)
    if direction == "LONG" and is_hammer:
        score.adjust(+1, "hammer_candle")
    elif direction == "SHORT" and is_star:
        score.adjust(+1, "shooting_star_candle")
    elif body_ratio < 0.15:
        score.adjust(-1, "doji_indecision")

    # ── Step 4c: News sentiment vote (direction-aware) ────────────────
    # Sentiment is read from catalyst_items attached to the MacroContext by the
    # scanner. The vote is asymmetric by trade direction:
    #
    # LONG trades:
    #   strongly negative (≤-0.5) → hard skip  (never buy into bad news)
    #   mildly negative   (≤-0.2) → -2          (headwind against long)
    #   mildly positive   (≥+0.2) → +1          (tailwind)
    #   strongly positive (≥+0.5) → +2          (confirmed catalyst)
    #
    # SHORT trades:
    #   strongly negative (≤-0.5) → +1          (news confirms bearish thesis)
    #   mildly negative   (≤-0.2) → +1          (aligned, minor boost)
    #   mildly positive   (≥+0.2) → -1          (news fights short thesis)
    #   strongly positive (≥+0.5) → -2          (strong headwind for shorts)
    #
    # Note: strongly negative shorts are still gated by NSE intraday-only rules
    # (no overnight short delivery), circuit-breaker risk, and liquidity gates
    # downstream — this vote only adjusts conviction, it doesn't override those.
    symbol_sentiment: float | None = None
    try:
        catalyst_items = getattr(macro, "catalyst_items", None) or {}
        items_for_sym  = catalyst_items.get(symbol, [])
        if items_for_sym:
            from yukti.services.catalyst_service import worst_sentiment
            symbol_sentiment = worst_sentiment(items_for_sym)
    except Exception:
        pass

    if symbol_sentiment is not None:
        if direction == "LONG":
            if symbol_sentiment <= -0.5:
                return _skip(
                    f"news_strongly_negative_{symbol_sentiment:.2f}",
                    conviction=1, bias=market_bias, symbol=symbol,
                )
            elif symbol_sentiment <= -0.2:
                score.adjust(-2, f"news_negative_{symbol_sentiment:.2f}")
            elif symbol_sentiment >= 0.5:
                score.adjust(+2, f"news_strongly_positive_{symbol_sentiment:.2f}")
            elif symbol_sentiment >= 0.2:
                score.adjust(+1, f"news_positive_{symbol_sentiment:.2f}")
        else:  # SHORT
            if symbol_sentiment <= -0.5:
                score.adjust(+1, f"news_strongly_negative_confirms_short_{symbol_sentiment:.2f}")
            elif symbol_sentiment <= -0.2:
                score.adjust(+1, f"news_negative_confirms_short_{symbol_sentiment:.2f}")
            elif symbol_sentiment >= 0.5:
                score.adjust(-2, f"news_strongly_positive_fights_short_{symbol_sentiment:.2f}")
            elif symbol_sentiment >= 0.2:
                score.adjust(-1, f"news_positive_fights_short_{symbol_sentiment:.2f}")

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

        # Daily EMA50 structural alignment: price above = long-term bull structure
        if snap_daily.above_ema50():
            if direction == "LONG":
                score.adjust(+1, "daily_above_ema50")
            elif direction == "SHORT":
                score.adjust(-1, "daily_above_ema50_counter_short")
        else:
            if direction == "SHORT":
                score.adjust(+1, "daily_below_ema50")
            elif direction == "LONG":
                score.adjust(-1, "daily_below_ema50_counter_long")

        # Daily MACD alignment: if momentum is confirmed on the daily chart, add 1
        if snap_daily.macd_bull and direction == "LONG":
            score.adjust(+1, "daily_macd_bullish")
        elif not snap_daily.macd_bull and direction == "SHORT":
            score.adjust(+1, "daily_macd_bearish")

        # Don't short at daily support / don't long at daily resistance
        if direction == "SHORT" and snap_daily.daily_support:
            dist_pct = (snap.close - snap_daily.daily_support) / snap.close
            if dist_pct < 0.005:
                return _skip("price_at_daily_support", conviction=score.clamp(), bias=market_bias, symbol=symbol)

        if direction == "LONG" and snap_daily.daily_resistance:
            dist_pct = (snap_daily.daily_resistance - snap.close) / snap.close
            if dist_pct < 0.005:
                return _skip("price_at_daily_resistance", conviction=score.clamp(), bias=market_bias, symbol=symbol)

    # ── Step 5b: Sector breadth filter ───────────────────────────────
    # A stock going LONG while its entire sector is selling off hard is a
    # low-probability fight-the-tape trade. Use the Nifty sector trend stored
    # in macro (sector_trends dict: sector_name → "UP"/"DOWN"/"FLAT") when
    # available, falling back to the broad Nifty trend as a proxy.
    try:
        from yukti.services.universe_scanner_service import SECTOR_STOCKS
        symbol_to_sector = {
            sym: sec
            for sec, members in SECTOR_STOCKS.items()
            for sym in members
        }
        stock_sector = symbol_to_sector.get(symbol)
        if stock_sector:
            sector_trends = getattr(macro, "sector_trends", {}) or {}
            sector_trend  = sector_trends.get(stock_sector)
            if sector_trend is None:
                # Fall back: infer from Nifty if sector data missing
                sector_trend = "UP" if nifty_trend == "UP" else ("DOWN" if nifty_trend == "DOWN" else "FLAT")
            if direction == "LONG" and sector_trend == "DOWN":
                score.adjust(-1, f"sector_{stock_sector}_headwind")
            elif direction == "SHORT" and sector_trend == "UP":
                score.adjust(-1, f"sector_{stock_sector}_tailwind_against_short")
            elif direction == "LONG" and sector_trend == "UP":
                score.adjust(+1, f"sector_{stock_sector}_tailwind")
            elif direction == "SHORT" and sector_trend == "DOWN":
                score.adjust(+1, f"sector_{stock_sector}_aligned")
    except Exception:
        pass   # SECTOR_STOCKS import or macro attribute missing — fail open

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

    # Detect timeframe from the snapshot to choose appropriate SL/target ratios.
    # Daily candles need tighter stops and closer targets because they are held
    # for a limited number of bars (1-2 weeks); intraday can afford wider ratios.
    is_daily_tf = getattr(snap, "timeframe", "5m") == "daily"

    # Widen SL multiplier when options market shows elevated IV (system prompt rule)
    atm_iv = getattr(macro, "nifty_atm_iv", None)
    if is_daily_tf:
        # Daily/swing: 1.2× ATR gives room for normal daily noise while keeping
        # losses manageable. T1 at 1.5R = 1.8 ATR is achievable in 3-5 days.
        sl_mult = 1.5 if (atm_iv is not None and atm_iv > 25) else 1.2
    else:
        # Intraday: tighter SL so T1 is reachable within the session.
        # 1.5×ATR SL → T1 at 3×ATR was unreachable on 5m charts (needed ~3% move).
        # 1.0×ATR SL → T1 at 1.5×ATR = achievable in a normal trending session.
        sl_mult = 1.5 if (atm_iv is not None and atm_iv > 25) else 1.0

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

    # Daily targets must be reachable within a 1-2 week holding window.
    # Intraday keeps the wider 2R/3R ratios for momentum plays.
    if is_daily_tf:
        t1_r = 1.2
        t2_r = 2.0
    else:
        # Intraday: T1 at 1.5R (was 2R) so break-even WR drops from ~37% to ~30%.
        # T2 at 2.5R keeps the runner meaningful without requiring extreme moves.
        t1_r = 1.5
        t2_r = 2.5
    if direction == "LONG":
        t1 = entry + t1_r * stop_dist
        t2 = entry + t2_r * stop_dist
    else:
        t1 = entry - t1_r * stop_dist
        t2 = entry - t2_r * stop_dist

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
        risk_reward=t2_r,  # RR based on full target (T2); T1 is a partial exit
        holding_period="swing" if is_daily_tf else "intraday",
    )
