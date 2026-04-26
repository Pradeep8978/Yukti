"""
yukti/jobs/meta_lessons.py

Daily job to aggregate top `key_lesson` values from high-quality journal
reflections and persist a small JSON summary to `data/meta_lessons.json`.

This is intended as a fast cache that can be injected into prompts or
displayed on dashboards. It avoids a DB scan at runtime when injecting
meta-lessons into prompts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict

from sqlalchemy import text as sa_text

from yukti.config import settings
from yukti.data.database import get_db

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("data") / "meta_lessons.json"


async def generate_meta_lessons(limit: int | None = None) -> Dict[str, Any]:
    """Aggregate top `key_lesson` values and write JSON to disk.

    Returns the JSON payload written.
    """
    limit = limit or getattr(settings, "rag_max_retrieved_items", 4)
    cutoff_days = getattr(settings, "rag_recency_days", 90)
    min_q = getattr(settings, "rag_min_quality_score", 6)
    cutoff = datetime.utcnow() - timedelta(days=cutoff_days)

    sql = sa_text(
        """
        SELECT COALESCE(one_actionable_lesson, key_lesson) as lesson, COUNT(*) as cnt
        FROM journal_entries
        WHERE (one_actionable_lesson IS NOT NULL OR key_lesson IS NOT NULL) AND quality_score >= :min_q AND created_at >= :cutoff
        GROUP BY lesson
        ORDER BY cnt DESC
        LIMIT :limit
        """
    )

    async with get_db() as db:
        rows = (await db.execute(sql, {"min_q": min_q, "cutoff": cutoff, "limit": limit})).fetchall()

    lessons = [{"lesson": r.lesson, "count": int(r.cnt)} for r in rows]
    payload = {"generated_at": datetime.utcnow().isoformat(), "lessons": lessons}

    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DEFAULT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    log.info("Wrote meta lessons to %s (count=%d)", DEFAULT_PATH, len(lessons))
    return payload


def read_meta_lessons() -> Dict[str, Any]:
    """Read the last-generated meta lessons summary from disk.

    Returns a dict with keys `generated_at` and `lessons`.
    """
    if not DEFAULT_PATH.exists():
        return {"generated_at": None, "lessons": []}
    try:
        with open(DEFAULT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("Failed to read meta lessons file")
        return {"generated_at": None, "lessons": []}


if __name__ == "__main__":
    # CLI entry for manual runs: `python -m yukti.jobs.meta_lessons`
    asyncio.run(generate_meta_lessons())
