"""
yukti/signals/context.py
Builds the full prompt context string sent to Claude each trading cycle.
"""
from __future__ import annotations

from datetime import datetime

from yukti.signals.indicators import IndicatorSnapshot


def compute_alignment(
    indicators_daily: IndicatorSnapshot | None,
    direction: str,
) -> str:
    """
    Compute alignment between daily trend and trade direction.
    Returns: ALIGNED | COUNTER-TREND | NEUTRAL
    """
    if indicators_daily is None:
        return "NEUTRAL"

    daily_trend = indicators_daily.trend
    if daily_trend == "SIDEWAYS":
        return "NEUTRAL"

    if direction == "LONG" and daily_trend == "UPTREND":
        return "ALIGNED"
    if direction == "SHORT" and daily_trend == "DOWNTREND":
        return "ALIGNED"
    if direction == "LONG" and daily_trend == "DOWNTREND":
        return "COUNTER-TREND"
    if direction == "SHORT" and daily_trend == "UPTREND":
        return "COUNTER-TREND"

    return "NEUTRAL"


def build_context(
    symbol: str,
    snap: IndicatorSnapshot,
    macro: "MacroContext",
    perf: dict,
    past_journal: str = "",
    symbol_headlines: list[str] | None = None,
    indicators_daily: IndicatorSnapshot | None = None,
    or_high: float | None = None,
    or_low: float | None = None,
) -> str:
    """
    Assembles the complete prompt context for Claude.

    Args:
        symbol:           Stock symbol e.g. "RELIANCE"
        snap:             IndicatorSnapshot from indicators.compute()
        macro:            MacroContext from services.macro_context_service.fetch_macro_context()
        perf:             Performance state dict from state.get_performance_state()
        past_journal:     Similar past trade journal (from memory.retrieve)
        symbol_headlines: Headlines filtered to this symbol/sector (optional)
        indicators_daily: IndicatorSnapshot from daily candles (optional)
        or_high:          Opening range high (optional)
        or_low:           Opening range low (optional)
    """
    # TYPE_CHECKING-style import to keep circular imports safe at runtime
    from yukti.services.macro_context_service import MacroContext  # noqa: F401

    vol_label = (
        "(HIGH — confirms move)" if snap.volume_ratio > 1.5
        else "(LOW — caution)"   if snap.volume_ratio < 0.7
        else "(average)"
    )

    rsi_note = (
        "← OVERBOUGHT — short bias" if snap.rsi_overbought()
        else "← OVERSOLD — long bias" if snap.rsi_oversold()
        else ""
    )

    macd_note = "→ BULLISH crossover" if snap.macd_bull else "→ BEARISH crossover"

    loss_note = ""
    if perf["consecutive_losses"] >= 3:
        loss_note = f"  ⚠️  {perf['consecutive_losses']} consecutive losses — raise conviction threshold to 9"
    if perf["daily_pnl_pct"] <= -2.0:
        loss_note = "  🛑 Daily loss limit hit — output SKIP with skip_reason='daily_loss_limit_hit'"
    if perf["daily_pnl_pct"] >= 3.0:
        loss_note = "  ✅ Great day. Protect gains — be conservative."

    # ── Daily timeframe section ──────────────────────────────────────
    daily_section = ""
    if indicators_daily is not None:
        adx_str = f"{indicators_daily.adx:.0f}" if indicators_daily.adx is not None else "N/A"
        adx_label = ""
        if indicators_daily.adx is not None:
            if indicators_daily.adx > 25:
                adx_label = " = strong trend"
            elif indicators_daily.adx > 20:
                adx_label = " = moderate trend"
            else:
                adx_label = " = weak/no trend"

        sup_str = f"₹{indicators_daily.daily_support:.2f}" if indicators_daily.daily_support else "N/A"
        res_str = f"₹{indicators_daily.daily_resistance:.2f}" if indicators_daily.daily_resistance else "N/A"

        st_daily = "BULLISH" if indicators_daily.supertrend_bull else "BEARISH"

        # Alignment: compute based on 5-min trend as a proxy for direction
        snap_dir = "LONG" if snap.trend == "UPTREND" else "SHORT" if snap.trend == "DOWNTREND" else ""
        alignment_label = compute_alignment(indicators_daily, snap_dir) if snap_dir else "NEUTRAL"

        daily_section = f"""
╔══ DAILY TIMEFRAME (Big Picture) ═══════════════════════════════╗
  Trend            : {indicators_daily.trend} (EMA20 {'>' if indicators_daily.ema20 > indicators_daily.ema50 else '<'} EMA50, ADX {adx_str}{adx_label})
  Key Resistance   : {res_str}
  Key Support      : {sup_str}
  RSI(14)          : {indicators_daily.rsi:.1f}
  Supertrend       : {st_daily}
  Alignment        : {alignment_label}
╚════════════════════════════════════════════════════════════════╝
"""

    # ── ORB + VWAP section ───────────────────────────────────────────
    or_parts = []
    if or_high is not None and or_low is not None:
        or_range = or_high - or_low
        or_pct = (or_range / or_low * 100) if or_low > 0 else 0
        or_parts.append(f"  Opening Range    : ₹{or_low:.2f} – ₹{or_high:.2f} (range: ₹{or_range:.2f}, {or_pct:.1f}%)")
    vwap_vs = ((snap.close - snap.vwap) / snap.vwap * 100) if snap.vwap > 0 else 0
    vwap_side = "above" if snap.close > snap.vwap else "below"
    or_parts.append(f"  VWAP             : ₹{snap.vwap:.2f} | Price vs VWAP: {vwap_vs:+.1f}% ({vwap_side})")
    orb_vwap_section = "\n".join(or_parts)

    return f"""
╔══ MARKET CONTEXT ══════════════════════════════════════════════╗
  Nifty50 change   : {macro.nifty_chg_pct:+.2f}%
  Nifty trend      : {macro.nifty_trend}
  India VIX        : {macro.vix_label}
  FII flows today  : {macro.fii_label}
  DII flows today  : {macro.dii_label}
  Time (IST)       : {datetime.now().strftime("%H:%M")}
  Headlines        :
{macro.headlines_text}
╚════════════════════════════════════════════════════════════════╝

╔══ YOUR PERFORMANCE STATE ══════════════════════════════════════╗
  Consecutive losses : {perf["consecutive_losses"]}
  Today P&L          : {perf["daily_pnl_pct"]:+.2f}%
  Win rate (last 10) : {perf["win_rate_last_10"]:.0%}
  Trades today       : {perf["trades_today"]}
  {loss_note}
╚════════════════════════════════════════════════════════════════╝
{daily_section}
╔══ STOCK: {symbol} ═══════════════════════════════════════════════╗
  Price            : ₹{snap.close:.2f}
  Candle change    : {snap.candle_change_pct:+.2f}%
  Volume vs 20 avg : {snap.volume_ratio:.1f}× {vol_label}
  Primary trend    : {snap.trend}

  ── Indicators ──
  RSI(14)          : {snap.rsi:.1f}  {rsi_note}
  MACD / Signal    : {snap.macd:.3f} / {snap.macd_sig:.3f}  {macd_note}
  MACD histogram   : {snap.macd_hist:.3f}
  ATR(14)          : ₹{snap.atr:.2f}
  Supertrend       : {"BULLISH" if snap.supertrend_bull else "BEARISH"} (₹{snap.supertrend:.2f})
  BB               : upper ₹{snap.bb_upper:.2f} | mid ₹{snap.bb_mid:.2f} | lower ₹{snap.bb_lower:.2f}

  ── Price vs levels ──
  vs VWAP  ₹{snap.vwap:.2f}  : {"ABOVE ✅" if snap.above_vwap()  else "BELOW ❌"}
  vs EMA20 ₹{snap.ema20:.2f} : {"ABOVE ✅" if snap.above_ema20() else "BELOW ❌"}
  vs EMA50 ₹{snap.ema50:.2f} : {"ABOVE ✅" if snap.above_ema50() else "BELOW ❌"}

  ── Market structure ──
  Nearest swing high : ₹{snap.nearest_swing_high:.2f}
  Nearest swing low  : ₹{snap.nearest_swing_low:.2f}
  Distance to s.high : {(snap.nearest_swing_high - snap.close) / snap.close * 100:+.2f}%
  Distance to s.low  : {(snap.nearest_swing_low  - snap.close) / snap.close * 100:+.2f}%

  ── ORB / VWAP ──
{orb_vwap_section}
╚════════════════════════════════════════════════════════════════╝

╔══ STOCK-SPECIFIC NEWS ═════════════════════════════════════════╗
{chr(10).join('    • ' + h for h in symbol_headlines) if symbol_headlines else '  No relevant news found for this symbol.'}
╚════════════════════════════════════════════════════════════════╝

╔══ PAST SIMILAR SETUP ══════════════════════════════════════════╗
{past_journal if past_journal else "  No similar past setup found in memory."}
╚════════════════════════════════════════════════════════════════╝

Think step by step. Assess the market first. Then the stock.
Decide: LONG, SHORT, or SKIP. Be honest about your conviction.
Output ONLY valid JSON — no prose, no markdown, no explanation.
""".strip()
