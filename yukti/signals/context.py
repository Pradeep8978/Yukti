"""
yukti/signals/context.py
Builds the full prompt context string sent to Claude each trading cycle.
"""
from __future__ import annotations

from datetime import datetime

from yukti.signals.indicators import IndicatorSnapshot


def build_context(
    symbol: str,
    snap: IndicatorSnapshot,
    nifty_change_pct: float,
    nifty_trend: str,
    news_summary: str,
    perf: dict,
    past_journal: str = "",
) -> str:
    """
    Assembles the complete prompt context for Claude.

    Args:
        symbol:           Stock symbol e.g. "RELIANCE"
        snap:             IndicatorSnapshot from indicators.compute()
        nifty_change_pct: Nifty50 % change today
        nifty_trend:      "UP" | "DOWN" | "SIDEWAYS"
        news_summary:     Latest macro/stock news as plain text
        perf:             Performance state dict from state.get_performance_state()
        past_journal:     Similar past trade journal (from memory.retrieve)
    """
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

    return f"""
╔══ MARKET CONTEXT ══════════════════════════════════════════════╗
  Nifty50 change   : {nifty_change_pct:+.2f}%
  Nifty trend      : {nifty_trend}
  Time (IST)       : {datetime.now().strftime("%H:%M")}
  News / macro     : {news_summary}
╚════════════════════════════════════════════════════════════════╝

╔══ YOUR PERFORMANCE STATE ══════════════════════════════════════╗
  Consecutive losses : {perf["consecutive_losses"]}
  Today P&L          : {perf["daily_pnl_pct"]:+.2f}%
  Win rate (last 10) : {perf["win_rate_last_10"]:.0%}
  Trades today       : {perf["trades_today"]}
  {loss_note}
╚════════════════════════════════════════════════════════════════╝

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
╚════════════════════════════════════════════════════════════════╝

╔══ PAST SIMILAR SETUP ══════════════════════════════════════════╗
{past_journal if past_journal else "  No similar past setup found in memory."}
╚════════════════════════════════════════════════════════════════╝

Think step by step. Assess the market first. Then the stock.
Decide: LONG, SHORT, or SKIP. Be honest about your conviction.
Output ONLY valid JSON — no prose, no markdown, no explanation.
""".strip()
