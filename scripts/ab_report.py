"""
scripts/ab_report.py
Analyse the A/B test disagreement log and produce a comparison report.

Run after a week of paper trading with AI_PROVIDER=ab_test:
    uv run python scripts/ab_report.py
    uv run python scripts/ab_report.py --log logs/ab_disagreements.jsonl
    uv run python scripts/ab_report.py --since 2025-01-15
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def load_records(log_path: str, since: str | None = None) -> list[dict]:
    path = Path(log_path)
    if not path.exists():
        print(f"No log file found at {log_path}")
        print("Run with AI_PROVIDER=ab_test for at least a day first.")
        return []

    records = []
    cutoff  = datetime.fromisoformat(since) if since else None

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if cutoff:
                    ts = datetime.fromisoformat(rec.get("timestamp", "2000-01-01"))
                    if ts < cutoff:
                        continue
                records.append(rec)
            except json.JSONDecodeError:
                pass

    return records


def report(records: list[dict]) -> None:
    if not records:
        print("No disagreement records found.")
        return

    primary   = records[0]["primary_provider"]
    secondary = records[0]["secondary_provider"]

    total = len(records)

    # Disagreement types
    action_differs = sum(
        1 for r in records
        if r["primary_action"] != r["secondary_action"]
    )
    direction_differs = sum(
        1 for r in records
        if r["primary_direction"] != r["secondary_direction"]
        and r["primary_action"] == r["secondary_action"] == "TRADE"
    )
    conviction_gap = [
        abs(r["primary_conviction"] - r["secondary_conviction"])
        for r in records
    ]
    avg_conviction_gap = sum(conviction_gap) / len(conviction_gap)

    # Cases where primary says TRADE, secondary says SKIP (missed by secondary)
    p_trade_s_skip = [
        r for r in records
        if r["primary_action"] == "TRADE" and r["secondary_action"] == "SKIP"
    ]
    # Cases where primary says SKIP, secondary says TRADE
    p_skip_s_trade = [
        r for r in records
        if r["primary_action"] == "SKIP" and r["secondary_action"] == "TRADE"
    ]

    # Latency comparison
    avg_primary_latency   = sum(r["primary_latency_ms"]   for r in records) / total
    avg_secondary_latency = sum(r["secondary_latency_ms"] for r in records) / total

    # Cost comparison
    total_primary_cost   = sum(r["primary_cost_usd"]   for r in records)
    total_secondary_cost = sum(r["secondary_cost_usd"] for r in records)

    # Setup type breakdown — which setups cause most disagreement?
    setup_counts: dict[str, int] = defaultdict(int)
    for r in records:
        setup = r.get("primary_reasoning", "")[:40]
        setup_counts[setup] += 1

    print(f"""
╔══ YUKTI A/B TEST REPORT ══════════════════════════════════════╗
  Primary provider   : {primary.upper()}
  Secondary provider : {secondary.upper()}
  Total disagreements: {total}

  ── Decision alignment ──
  Action differs     : {action_differs} ({action_differs/total*100:.1f}%)
  Direction differs  : {direction_differs}
  Avg conviction gap : {avg_conviction_gap:.1f} points

  ── Trade / Skip conflicts ──
  {primary} TRADE, {secondary} SKIP : {len(p_trade_s_skip)} cases
     → {primary} found setups {secondary} missed
  {primary} SKIP, {secondary} TRADE : {len(p_skip_s_trade)} cases
     → {secondary} wanted to trade, {primary} said no

  ── Speed ──
  Avg {primary} latency   : {avg_primary_latency:.0f}ms
  Avg {secondary} latency : {avg_secondary_latency:.0f}ms
  Winner: {"DRAW" if abs(avg_primary_latency - avg_secondary_latency) < 200
           else (primary if avg_primary_latency < avg_secondary_latency else secondary).upper()}

  ── Cost (these {total} disagreement calls only) ──
  {primary} cost    : ${total_primary_cost:.4f}
  {secondary} cost  : ${total_secondary_cost:.4f}
  Savings if using {secondary}: ${abs(total_primary_cost - total_secondary_cost):.4f}
╚════════════════════════════════════════════════════════════════╝

Sample disagreements (first 5):
""")

    for i, r in enumerate(records[:5]):
        print(f"  #{r['call_n']:4d}  {r['timestamp'][:16]}")
        print(f"         {primary.upper():8s}: {r['primary_action']:5s} {r.get('primary_direction') or '—':5s} conv={r['primary_conviction']}")
        print(f"         {secondary.upper():8s}: {r['secondary_action']:5s} {r.get('secondary_direction') or '—':5s} conv={r['secondary_conviction']}")
        print(f"         {primary} said: {r['primary_reasoning'][:80]}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Yukti A/B test report")
    parser.add_argument("--log",   default="logs/ab_disagreements.jsonl")
    parser.add_argument("--since", default=None, help="ISO date e.g. 2025-01-15")
    args = parser.parse_args()

    records = load_records(args.log, args.since)
    report(records)


if __name__ == "__main__":
    main()
