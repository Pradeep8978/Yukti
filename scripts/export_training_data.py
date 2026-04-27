"""
scripts/export_training_data.py

Export structured training data from the Yukti DB into JSONL for
supervised or reward-model training pipelines.

This is a minimal, safe exporter — review and tailor the prompt/target
format to your chosen training objective before running.

Usage:
  python scripts/export_training_data.py --out data/training/journal_export.jsonl --since 2024-01-01

Note: This script expects the project's async `get_db()` context manager
and SQLAlchemy models to be available. It does not perform any training.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime

from sqlalchemy import select

from yukti.data.database import get_db
from yukti.data.models import JournalEntry, Trade, DecisionLog

try:
    # Optional reward helper — may be missing until trainer/rewards.py exists
    from trainer.rewards import compute_reward
except Exception:  # pragma: no cover - optional
    compute_reward = None


async def export(out_path: str, since: datetime | None = None, limit: int | None = None, min_quality: float | None = None) -> int:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    async with get_db() as db:
        q = select(JournalEntry, Trade).join(Trade, JournalEntry.trade_id == Trade.id)
        if since:
            q = q.where(JournalEntry.created_at >= since)
        q = q.order_by(JournalEntry.created_at)
        if limit:
            q = q.limit(limit)

        res = await db.execute(q)
        rows = res.fetchall()

        written = 0
        skipped_quality = 0
        with open(out_path, "w", encoding="utf-8") as fh:
            for row in rows:
                # row is a SQLAlchemy Row; elements are (JournalEntry, Trade)
                try:
                    j: JournalEntry = row[0]
                    t: Trade = row[1]
                except Exception:
                    continue

                # Quality gate: skip low-quality journals
                if min_quality is not None:
                    q_val = float(j.quality_score) if j.quality_score is not None else 0.0
                    if q_val < min_quality:
                        skipped_quality += 1
                        continue

                target = {
                    "direction": t.direction,
                    "setup_type": t.setup_type,
                    "conviction": t.conviction,
                    "pnl_pct": float(j.pnl_pct) if j.pnl_pct is not None else None,
                    "outcome": "WIN" if (j.pnl_pct or 0.0) > 0 else "LOSS",
                }

                # Attach the most recent DecisionLog for this symbol (if any)
                decision = None
                try:
                    dq = (
                        select(DecisionLog)
                        .where(DecisionLog.symbol == j.symbol, DecisionLog.decided_at <= j.created_at)
                        .order_by(DecisionLog.decided_at.desc())
                        .limit(1)
                    )
                    dres = await db.execute(dq)
                    drow = dres.scalars().first()
                    if drow:
                        decision = {
                            "action": drow.action,
                            "direction": drow.direction,
                            "conviction": int(drow.conviction) if drow.conviction is not None else None,
                            "market_bias": drow.market_bias,
                            "skip_reason": drow.skip_reason,
                            "decided_at": drow.decided_at.isoformat() if drow.decided_at else None,
                            "full_json": drow.full_json,
                        }
                except Exception:
                    decision = None

                reward_val = None
                if compute_reward is not None and j.pnl_pct is not None:
                    try:
                        reward_val = compute_reward(
                            float(j.pnl_pct),
                            "WIN" if (j.pnl_pct or 0.0) > 0 else "LOSS",
                            int(t.conviction or 5),
                            t.holding_period or "intraday",
                        )
                    except Exception:
                        reward_val = None

                obj = {
                    "journal_id": int(j.id),
                    "trade_id": int(j.trade_id),
                    "symbol": j.symbol,
                    "prompt": (t.reasoning or "") + "\n\nPOST_JOURNAL:\n" + (j.entry_text or ""),
                    "decision": decision,
                    "target": target,
                    "quality_score": float(j.quality_score) if j.quality_score is not None else None,
                    "reward": reward_val,
                    "created_at": j.created_at.isoformat() if j.created_at else None,
                }
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
                written += 1

    print(f"Exported {written} rows to {out_path}")
    if min_quality is not None:
        print(f"  (skipped {skipped_quality} rows with quality_score < {min_quality})")
    return written


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="data/training/journal_export.jsonl")
    p.add_argument("--since", default=None, help="ISO date e.g. 2024-01-01")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--min-quality", type=float, default=None,
                   help="Skip journals with quality_score below this threshold (e.g. 6)")
    args = p.parse_args()

    since = _parse_date(args.since)
    asyncio.run(export(args.out, since=since, limit=args.limit, min_quality=args.min_quality))


if __name__ == "__main__":
    main()
