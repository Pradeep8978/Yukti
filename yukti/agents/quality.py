"""
yukti/agents/quality.py
Decision quality tracker — validates that Arjun's conviction scores
actually correlate with trade outcomes.

If 9-conviction trades have the same win rate as 6-conviction trades,
the model is NOT using conviction meaningfully, and the prompt needs work.

Metrics tracked:
    - Skip rate (% of candles that became SKIP decisions)
    - Conviction distribution (histogram)
    - Win rate per conviction bucket
    - Average P&L per conviction bucket
    - Claude vs Gemini comparison (if A/B test mode)

Generate a report with:
    uv run python -m yukti.agents.quality
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from yukti.data.database import get_db
from yukti.data.models import Trade, DecisionLog

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Analyser
# ═══════════════════════════════════════════════════════════════

async def analyse_decision_quality(days: int = 30) -> dict[str, Any]:
    """
    Analyse the last `days` of decisions. Returns a dict of metrics
    suitable for JSON serialisation or Prometheus export.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with get_db() as db:
        # All decisions in period
        decision_result = await db.execute(
            select(DecisionLog).where(DecisionLog.decided_at >= cutoff)
        )
        decisions = list(decision_result.scalars().all())

        # All closed trades in period
        trade_result = await db.execute(
            select(Trade)
            .where(
                Trade.opened_at >= cutoff,
                Trade.pnl_pct.is_not(None),
            )
        )
        trades = list(trade_result.scalars().all())

    total_decisions = len(decisions)
    if total_decisions == 0:
        return {"error": "No decisions in period", "days": days}

    skips = [d for d in decisions if d.action == "SKIP"]
    skip_rate = len(skips) / total_decisions

    # ── Skip reasons breakdown ─────────────────────────────────
    skip_reason_counts: dict[str, int] = defaultdict(int)
    for s in skips:
        skip_reason_counts[s.skip_reason or "unspecified"] += 1

    # ── Conviction distribution & outcomes ─────────────────────
    conv_buckets: dict[int, dict] = {
        i: {"trades": 0, "wins": 0, "total_pnl_pct": 0.0}
        for i in range(1, 11)
    }

    for t in trades:
        b = conv_buckets[t.conviction]
        b["trades"] += 1
        if (t.pnl_pct or 0) > 0:
            b["wins"] += 1
        b["total_pnl_pct"] += t.pnl_pct or 0

    # Compute win rate and avg P&L per bucket
    for bucket in conv_buckets.values():
        bucket["win_rate"] = (
            bucket["wins"] / bucket["trades"]
            if bucket["trades"] > 0 else None
        )
        bucket["avg_pnl_pct"] = (
            bucket["total_pnl_pct"] / bucket["trades"]
            if bucket["trades"] > 0 else None
        )

    # ── Signal: is conviction predictive? ──────────────────────
    # Low conviction (5-6) vs high conviction (9-10) — compare win rates
    low_conv_trades  = [b for c, b in conv_buckets.items() if 5 <= c <= 6 and b["trades"] > 0]
    high_conv_trades = [b for c, b in conv_buckets.items() if 9 <= c <= 10 and b["trades"] > 0]

    low_wr  = (
        sum(b["wins"] for b in low_conv_trades) /
        sum(b["trades"] for b in low_conv_trades)
    ) if low_conv_trades else None
    high_wr = (
        sum(b["wins"] for b in high_conv_trades) /
        sum(b["trades"] for b in high_conv_trades)
    ) if high_conv_trades else None

    conviction_signal = "insufficient_data"
    if low_wr is not None and high_wr is not None:
        diff = high_wr - low_wr
        if diff >= 0.15:
            conviction_signal = "strong_predictive"
        elif diff >= 0.05:
            conviction_signal = "weakly_predictive"
        elif diff >= -0.05:
            conviction_signal = "no_signal"
        else:
            conviction_signal = "inverted"   # low conviction actually wins more!

    # ── Setup type breakdown ────────────────────────────────────
    setup_performance: dict[str, dict] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "total_pnl_pct": 0.0}
    )
    for t in trades:
        s = setup_performance[t.setup_type or "unknown"]
        s["trades"] += 1
        if (t.pnl_pct or 0) > 0:
            s["wins"] += 1
        s["total_pnl_pct"] += t.pnl_pct or 0

    for setup in setup_performance.values():
        setup["win_rate"]    = setup["wins"] / setup["trades"] if setup["trades"] else None
        setup["avg_pnl_pct"] = setup["total_pnl_pct"] / setup["trades"] if setup["trades"] else None

    return {
        "period_days":        days,
        "total_decisions":    total_decisions,
        "total_trades":       len(trades),
        "skip_rate":          round(skip_rate, 3),
        "skip_reasons":       dict(skip_reason_counts),
        "conviction_buckets": {
            str(c): {
                "trades":    b["trades"],
                "win_rate":  round(b["win_rate"], 3) if b["win_rate"] is not None else None,
                "avg_pnl_pct": round(b["avg_pnl_pct"], 3) if b["avg_pnl_pct"] is not None else None,
            }
            for c, b in conv_buckets.items()
        },
        "conviction_signal":  conviction_signal,
        "low_conv_win_rate":  round(low_wr, 3)  if low_wr  is not None else None,
        "high_conv_win_rate": round(high_wr, 3) if high_wr is not None else None,
        "setup_performance":  {
            k: {
                "trades":      v["trades"],
                "win_rate":    round(v["win_rate"], 3) if v["win_rate"] is not None else None,
                "avg_pnl_pct": round(v["avg_pnl_pct"], 3) if v["avg_pnl_pct"] is not None else None,
            }
            for k, v in setup_performance.items()
        },
    }


# ═══════════════════════════════════════════════════════════════
#  CLI — print a human-readable report
# ═══════════════════════════════════════════════════════════════

def _format_report(r: dict[str, Any]) -> str:
    lines = [
        "",
        "╔══ YUKTI DECISION QUALITY REPORT ══════════════════════════════╗",
        f"  Period             : last {r['period_days']} days",
        f"  Total decisions    : {r['total_decisions']}",
        f"  Total closed trades: {r['total_trades']}",
        f"  Skip rate          : {r['skip_rate']*100:.1f}%",
        "",
        "  ── Skip reasons ──",
    ]
    for reason, count in sorted(r["skip_reasons"].items(), key=lambda x: -x[1])[:6]:
        lines.append(f"    {reason:30s}: {count}")

    lines += ["", "  ── Conviction → outcomes ──"]
    lines.append(f"    conv  trades  win%     avg_P&L%")
    lines.append(f"    ──────────────────────────────")
    for c in range(1, 11):
        b = r["conviction_buckets"][str(c)]
        if b["trades"] == 0:
            continue
        wr = f"{b['win_rate']*100:>5.1f}%" if b["win_rate"] is not None else "  —  "
        ap = f"{b['avg_pnl_pct']:+.2f}%"   if b["avg_pnl_pct"] is not None else "  —  "
        lines.append(f"    {c:>4d}  {b['trades']:>6d}  {wr}  {ap:>8s}")

    lines += ["", "  ── Signal quality ──"]
    lines.append(f"    Low conv (5-6) win rate : {(r['low_conv_win_rate'] or 0)*100:.1f}%")
    lines.append(f"    High conv (9-10) win rate: {(r['high_conv_win_rate'] or 0)*100:.1f}%")
    lines.append(f"    Verdict                 : {r['conviction_signal'].upper().replace('_',' ')}")

    if r["conviction_signal"] == "no_signal":
        lines.append("    ⚠ Conviction scores are NOT predicting outcomes.")
        lines.append("      Consider reviewing/revising the system prompt.")
    elif r["conviction_signal"] == "inverted":
        lines.append("    🚨 Low-conviction trades are WINNING MORE than high-conviction!")
        lines.append("      Strong signal that the prompt or pre-filter has a bug.")
    elif r["conviction_signal"] == "strong_predictive":
        lines.append("    ✅ Conviction is meaningfully predicting outcomes. Good.")

    lines += ["", "  ── Setup type performance ──"]
    lines.append(f"    setup_type                   trades  win%     avg_P&L%")
    lines.append(f"    ──────────────────────────────────────────────────────")
    for setup, perf in sorted(r["setup_performance"].items(), key=lambda x: -x[1]["trades"]):
        if perf["trades"] == 0:
            continue
        wr = f"{perf['win_rate']*100:>5.1f}%" if perf["win_rate"] is not None else "  —  "
        ap = f"{perf['avg_pnl_pct']:+.2f}%"   if perf["avg_pnl_pct"] is not None else "  —  "
        lines.append(f"    {setup:30s}  {perf['trades']:>6d}  {wr}  {ap:>8s}")

    lines += ["", "╚════════════════════════════════════════════════════════════════╝", ""]
    return "\n".join(lines)


async def _main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Yukti decision quality report")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    report = await analyse_decision_quality(args.days)
    print(_format_report(report))


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
