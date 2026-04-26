"""
yukti/agents/memory.py
Advanced hybrid retrieval and journal storage for Yukti.

Features:
- Structured `store_journal` accepting `JournalReflection`.
- `retrieve_similar` implements hybrid scoring: vector similarity + metadata filters,
  outcome-weighting, recency decay, and simple diversity heuristic.
- `retrieve_similar_hybrid` (Enhanced version) directly leveraging SQL for weights.
- Emission of Prometheus metrics for observability.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, List, Optional

import voyageai
from sqlalchemy import text as sa_text

from yukti.config import settings
from yukti.agents.rag_schemas import JournalReflection, RetrievedTradeContext, RetrievalMetadata, RagSettings
from yukti.metrics import (
    rag_retrieval_count,
    rag_avg_similarity,
    rag_quality_score_avg,
)

log = logging.getLogger(__name__)

# Use central metrics exported from yukti.metrics for observability
from yukti.metrics import (
    rag_retrieval_count,
    rag_avg_similarity,
    rag_quality_score_avg,
)


_voyage_client: voyageai.Client | None = None


def _voyage() -> voyageai.Client:
    global _voyage_client
    if _voyage_client is None:
        _voyage_client = voyageai.Client(api_key=settings.voyage_api_key)
    return _voyage_client


async def _embed(texts: List[str], input_type: str = "document") -> List[List[float]]:
    """Async wrapper around the synchronous Voyage AI embedding call."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: _voyage().embed(texts, model="voyage-large-2-instruct", input_type=input_type),
    )
    return result.embeddings


async def embed_journal(journal_text: str) -> List[float]:
    """Generate a single 1024-dim embedding for a journal entry."""
    embeddings = await _embed([journal_text], input_type="document")
    return embeddings[0]


async def store_journal(
    trade_id: int,
    symbol: str,
    setup_type: str,
    direction: str,
    pnl_pct: float,
    journal: JournalReflection | str,
    conviction: int = 5,
) -> None:
    """Embed a journal reflection and persist it to PostgreSQL with pgvector.

    `journal` may be a `JournalReflection` model (preferred) or a raw string.
    If the reflection's `quality_score` is below config `rag_min_quality_score`,
    the entry is marked `discarded=True` to exclude it from retrieval.
    """
    from yukti.data.database import get_db
    from yukti.data.models import JournalEntry

    # Normalize reflection to JournalReflection
    if isinstance(journal, str):
        refl = JournalReflection(
            setup_summary=journal,
            outcome="BREAKEVEN",
            reason="",
            one_actionable_lesson="",
            quality_score=0,
            market_regime=None,
            setup_type=setup_type,
            created_at=datetime.utcnow(),
        )
    else:
        refl = journal

    # Build a concise text to embed (include summary, reason, lesson)
    embed_text = (
        (refl.setup_summary or "") + "\nReason: " + (refl.reason or "") + "\nLesson: " + (refl.one_actionable_lesson or "")
    )

    try:
        embedding = await embed_journal(embed_text)
    except Exception as exc:
        log.warning("Embedding failed for trade %d: %s", trade_id, exc)
        embedding = None

    discarded = False
    min_q = getattr(settings, "rag_min_quality_score", 6)
    if (refl.quality_score or 0) < min_q:
        discarded = True

    # Determine outcome
    outcome = "WIN" if pnl_pct > 0.5 else "LOSS" if pnl_pct < -0.5 else "BREAKEVEN"

    async with get_db() as db:
        db.add(JournalEntry(
            trade_id=trade_id,
            symbol=symbol,
            setup_type=setup_type,
            direction=direction,
            pnl_pct=pnl_pct,
            entry_text=(refl.setup_summary or "")[:2000],
            setup_summary=refl.setup_summary,
            embedding=embedding,
            quality_score=refl.quality_score,
            # populate both legacy and new fields for compatibility
            key_lesson=(refl.one_actionable_lesson or None),
            market_regime=refl.market_regime,
            outcome_reason=(refl.reason or None),
            one_actionable_lesson=(refl.one_actionable_lesson or None),
            outcome=refl.outcome or outcome,
            is_high_conviction=(conviction >= 8),
            discarded=discarded,
        ))

    log.info("Journal stored for trade %d (%s %s %.2f%%) quality=%s discarded=%s", trade_id, symbol, direction, pnl_pct, refl.quality_score, discarded)


async def retrieve_similar(
    symbol: str,
    setup_type: str,
    direction: str,
    top_k: int = 4,
) -> str:
    """
    Hybrid retrieval: vector similarity + metadata scoring.

    Returns a formatted string suitable for prompt injection. Also emits
    Prometheus metrics for observability.
    """
    from yukti.data.database import get_db

    # Configurable settings with sensible defaults
    cfg = RagSettings(
        max_retrieved_items=getattr(settings, "rag_max_retrieved_items", 4),
        recency_days=getattr(settings, "rag_recency_days", 90),
        min_quality_score=getattr(settings, "rag_min_quality_score", 6),
        outcome_weight_win=getattr(settings, "rag_outcome_weight_win", 1.2),
        recency_half_life_days=getattr(settings, "rag_recency_half_life_days", 365),
        max_fetch_candidates=getattr(settings, "rag_max_fetch_candidates", 50),
        diversity_lambda=getattr(settings, "rag_diversity_lambda", 0.7),
    )

    query_text = f"{symbol} {direction} {setup_type} trade on NSE"

    try:
        [query_emb] = await _embed([query_text], input_type="query")
    except Exception as exc:
        log.warning("RAG: embedding failed for query '%s': %s — falling back to simple DB filter", query_text, exc)
        # Fallback: return most recent same-symbol entries
        async with get_db() as db:
            sql_fb = sa_text("""
                SELECT id, trade_id, entry_text, pnl_pct, setup_type, direction, symbol,
                       quality_score, one_actionable_lesson, setup_summary, outcome, reason, created_at
                FROM journal_entries
                WHERE symbol = :symbol AND embedding IS NOT NULL
                ORDER BY created_at DESC
                LIMIT :k
            """)
            rows = (await db.execute(sql_fb, {"symbol": symbol, "k": top_k})).fetchall()
        if not rows:
            return ""
        parts = []
        for i, row in enumerate(rows[:top_k]):
            outcome = "WIN" if (row.pnl_pct or 0) > 0 else "LOSS"
            setup = getattr(row, "setup_summary", None) or getattr(row, "entry_text", "(no summary)")
            lesson = getattr(row, "one_actionable_lesson", None) or getattr(row, "key_lesson", None) or "—"
            why = f"fallback_recent_{i+1}"
            parts.append(
                f"{i+1}. {row.symbol} | {row.setup_type or 'unknown'} | {outcome} {row.pnl_pct:+.1f}% | sim={getattr(row,'similarity',0.0):.2f}\n"
                f"   - Setup   : {setup}\n"
                f"   - Outcome : {getattr(row,'reason', '') or ''}\n"
                f"   - Lesson  : {lesson}\n"
                f"   - Retrieved because: {why}"
            )

        header = "=== Past Similar Trades for Learning ==="
        return header + "\n\n" + "\n\n".join(parts)

    # Fetch a larger candidate set from DB to allow re-ranking with metadata
    fetch_n = max(cfg.max_retrieved_items * 6, cfg.max_fetch_candidates)

    sql = sa_text("""
        SELECT id, trade_id, symbol, setup_type, direction, pnl_pct,
               entry_text, setup_summary, outcome, reason, 
               one_actionable_lesson, quality_score, market_regime,
               is_high_conviction, created_at,
               1 - (embedding <=> :emb ::vector) AS similarity
        FROM   journal_entries
        WHERE  embedding IS NOT NULL AND (discarded IS NULL OR discarded = FALSE)
        ORDER  BY embedding <=> :emb ::vector
        LIMIT  :n
    """)

    async with get_db() as db:
        try:
            rows = (await db.execute(sql, {"emb": str(query_emb), "n": fetch_n})).fetchall()
        except Exception as exc:
            log.warning("RAG DB query failed: %s", exc)
            return ""

    candidates: List[RetrievedTradeContext] = []
    now = datetime.utcnow()
    similarities = []
    qualities = []

    for row in rows:
        # Skip low-quality reflections early
        qscore = int(getattr(row, "quality_score", 0) or 0)
        if qscore < cfg.min_quality_score:
            continue

        sim = float(getattr(row, "similarity", 0.0) or 0.0)
        age_days = (now - getattr(row, "created_at", now)).days if getattr(row, "created_at", None) else 3650
        decay = 0.5 ** (age_days / max(1.0, cfg.recency_half_life_days))
        outcome_weight = cfg.outcome_weight_win if (getattr(row, "pnl_pct", 0.0) or 0.0) > 0 else 1.0
        symbol_bonus = 1.15 if (getattr(row, "symbol", "") or "") == symbol else 1.0
        recency_bonus = 1.1 if age_days <= cfg.recency_days else 1.0
        quality_mul = (qscore / 10.0) if qscore > 0 else 0.5

        final_score = sim * outcome_weight * symbol_bonus * decay * recency_bonus * quality_mul

        retrieval_reason = (
            f"sim={sim:.2f},q={qscore},outcome={'win' if (getattr(row,'pnl_pct',0) or 0)>0 else 'loss'},"
            f"age_days={age_days},decay={decay:.2f},sym_bonus={symbol_bonus:.2f}"
        )

        ctx = RetrievedTradeContext(
            journal_id=getattr(row, "id", None),
            trade_id=getattr(row, "trade_id", None),
            symbol=getattr(row, "symbol", None),
            setup_type=getattr(row, "setup_type", None),
            direction=getattr(row, "direction", None),
            pnl_pct=float(getattr(row, "pnl_pct", 0.0) or 0.0),
            similarity=sim,
            quality_score=qscore,
            one_actionable_lesson=getattr(row, "one_actionable_lesson", None) or getattr(row, "key_lesson", None),
            reason=getattr(row, "reason", None) or getattr(row, "outcome_reason", None),
            created_at=getattr(row, "created_at", None),
            retrieval_reason=retrieval_reason,
        )
        candidates.append((final_score, ctx))
        similarities.append(sim)
        qualities.append(qscore)

    if not candidates:
        return ""

    # Sort by final_score desc
    candidates.sort(key=lambda t: t[0], reverse=True)

    # Diversity heuristic: prefer at least one winning trade and avoid >2 losses
    max_losses = min(2, cfg.max_retrieved_items)
    selected: List[RetrievedTradeContext] = []
    losses = 0
    wins = 0

    for score, ctx in candidates:
        if len(selected) >= cfg.max_retrieved_items:
            break
        is_win = (ctx.pnl_pct or 0.0) > 0
        # If too many losses already and there are wins later, skip this loss
        if not is_win and losses >= max_losses:
            # check if future wins exist
            future_has_win = any((c[1].pnl_pct or 0.0) > 0 for c in candidates if c[0] < score)
            if future_has_win:
                continue

        selected.append(ctx)
        if is_win:
            wins += 1
        else:
            losses += 1

    # Emit metrics
    try:
        rag_retrieval_count.inc()
        if similarities:
            rag_avg_similarity.set(sum(similarities) / len(similarities))
        if qualities:
            rag_quality_score_avg.set(sum(qualities) / len(qualities))
    except Exception:
        pass

    # Build formatted injection string
    parts: List[str] = []
    for i, ctx in enumerate(selected):
        outcome = "WIN" if (ctx.pnl_pct or 0) > 0 else "LOSS"
        entry_summary = (ctx.one_actionable_lesson or ctx.reason or "").strip() or "(no summary)"
        why = ctx.retrieval_reason or ""

        parts.append(
            f"{i+1}. {ctx.symbol} | {ctx.setup_type or 'unknown'} | {outcome} {ctx.pnl_pct:+.1f}% | sim={ctx.similarity:.2f}\\n"
            f"   - Setup summary : {entry_summary}\\n"
            f"   - What happened : {ctx.reason or 'See journal entry.'}\\n"
            f"   - Key lesson    : {ctx.one_actionable_lesson or '—'}\\n"
            f"   - Retrieved because: {why}"
        )

    # Meta lessons: simple frequency of key_lesson in recent journals
    meta = ""
    try:
        recent_cutoff = datetime.utcnow() - timedelta(days=cfg.recency_days)
        sql_meta = sa_text("""
            SELECT COALESCE(one_actionable_lesson, key_lesson) as lesson, COUNT(*) as cnt
            FROM journal_entries
            WHERE (one_actionable_lesson IS NOT NULL OR key_lesson IS NOT NULL) AND quality_score >= :min_q AND created_at >= :cutoff
            GROUP BY lesson
            ORDER BY cnt DESC
            LIMIT 3
        """)
        async with get_db() as db:
            rows_meta = (await db.execute(sql_meta, {"min_q": cfg.min_quality_score, "cutoff": recent_cutoff})).fetchall()
        if rows_meta:
            lessons = [f"{r.lesson} ({r.cnt})" for r in rows_meta]
            meta = "Meta Lessons Learned: " + ", ".join(lessons)
    except Exception:
        meta = ""

    header = f"Past Similar Trades (top {len(selected)}):"
    body = "\\n\\n".join(parts)
    if meta:
        body = body + "\\n\\n" + meta

    # Log concise retrieval info
    if selected:
        top = selected[0]
        top_match = (
            f"{top.symbol} - similarity {top.similarity:.2f} - outcome: {'win' if (top.pnl_pct or 0)>0 else 'loss'}"
            f" - lesson: {top.one_actionable_lesson or top.reason or '—'}"
        )
        log.info("Retrieved %d past trades. Top match: %s", len(selected), top_match)

    return header + "\\n\\n" + body


# ─────────────────────────────────────────────────────────────
# Advanced Hybrid Retrieval (From Enhanced Commit)
# ─────────────────────────────────────────────────────────────

@dataclass
class RetrievedJournal:
    """A retrieved journal entry with metadata for hybrid retrieval."""
    trade_id: int
    symbol: str
    setup_type: str
    direction: str
    pnl_pct: float
    entry_text: str
    setup_summary: Optional[str]
    outcome: str  # WIN | LOSS | BREAKEVEN
    reason: Optional[str]
    one_actionable_lesson: Optional[str]
    quality_score: Optional[float]
    market_regime: Optional[str]
    is_high_conviction: bool
    similarity: float
    created_at: datetime
    why_selected: str  # Human-readable reason for selection


def _get_rag_config() -> dict:
    """Get RAG configuration from settings with sensible defaults."""
    return {
        "max_retrieved": getattr(settings, "rag_max_retrieved", 4),
        "min_quality_score": getattr(settings, "rag_min_quality_score", 6.0),
        "recency_days": getattr(settings, "rag_recency_days", 90),
        "outcome_weight": getattr(settings, "rag_outcome_weight", 0.15),
        "recent_decay": getattr(settings, "rag_recent_decay", 0.02),
    }


async def retrieve_similar_hybrid(
    symbol:     str,
    setup_type: str,
    direction:  str,
    market_regime: Optional[str] = None,
    top_k:      Optional[int] = None,
) -> list[RetrievedJournal]:
    """
    Advanced hybrid retrieval combining vector similarity with metadata filters.
    
    Features:
    - Vector similarity (cosine) as primary ranking
    - Metadata filters: recency (last 90 days), quality score >= 6
    - Outcome weighting: winning trades boosted by configured weight
    - Recency decay: configured decay per week to favor recent trades
    - Diverse results: avoid too many similar losing trades
    - Returns metadata including similarity score and why selected
    """
    config = _get_rag_config()
    max_retrieved = top_k or config["max_retrieved"]
    min_quality = config["min_quality_score"]
    recency_days = config["recency_days"]
    outcome_weight = config["outcome_weight"]
    
    from yukti.data.database import get_db

    query_text = f"{symbol} {direction} {setup_type} equity trade NSE"

    # Generate query embedding
    try:
        [query_emb] = await _embed([query_text], input_type="query")
    except Exception as exc:
        log.warning("Hybrid retrieval embedding failed: %s", exc)
        rag_retrieval_count.inc()
        return []

    # Calculate recency cutoff
    recency_cutoff = datetime.utcnow() - timedelta(days=recency_days)

    # SQL with hybrid scoring: vector similarity + outcome weight + recency decay
    sql = sa_text("""
        WITH base_results AS (
            SELECT 
                id, trade_id, symbol, setup_type, direction, pnl_pct,
                entry_text, setup_summary, outcome, reason, 
                one_actionable_lesson, quality_score, market_regime,
                is_high_conviction, created_at,
                1 - (embedding <=> :emb ::vector) AS base_similarity
            FROM journal_entries
            WHERE embedding IS NOT NULL
              AND created_at >= :recency_cutoff
              AND (quality_score IS NULL OR quality_score >= :min_quality)
        )
        SELECT 
            id, trade_id, symbol, setup_type, direction, pnl_pct,
            entry_text, setup_summary, outcome, reason,
            one_actionable_lesson, quality_score, market_regime,
            is_high_conviction, created_at,
            base_similarity,
            -- Outcome weight: winning trades get a boost
            CASE 
                WHEN outcome = 'WIN' THEN base_similarity + :outcome_weight
                WHEN outcome = 'LOSS' THEN base_similarity - (:outcome_weight * 0.5)
                ELSE base_similarity
            END AS weighted_similarity,
            -- Recency decay: ~2% per week
            EXTRACT(EPOCH FROM (now() - created_at)) / 604800.0 AS weeks_old
        FROM base_results
        ORDER BY weighted_similarity DESC, base_similarity DESC
        LIMIT :limit
    """)

    try:
        async with get_db() as db:
            rows = (await db.execute(sql, {
                "emb": str(query_emb),
                "recency_cutoff": recency_cutoff,
                "min_quality": min_quality,
                "outcome_weight": outcome_weight,
                "limit": max_retrieved * 2,  # Get more for diversity filtering
            })).fetchall()

        # Apply diversity filtering: ensure mix of WIN/LOSS outcomes
        results: list[RetrievedJournal] = []
        seen_outcomes = {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0}
        max_same_outcome = 2  # Max 2 from same outcome category

        for row in rows:
            if len(results) >= max_retrieved:
                break
                
            outcome = row.outcome or ("WIN" if row.pnl_pct > 0 else "LOSS")
            
            # Diversity check
            if seen_outcomes.get(outcome, 0) >= max_same_outcome:
                # Allow if it's a high-quality or high-conviction trade
                if not (row.is_high_conviction or (row.quality_score and row.quality_score >= 8)):
                    continue
            
            seen_outcomes[outcome] = seen_outcomes.get(outcome, 0) + 1

            # Build why_selected explanation
            why_parts = []
            why_parts.append(f"similarity={row.base_similarity:.2f}")
            if outcome == "WIN":
                why_parts.append("winning trade")
            if row.is_high_conviction:
                why_parts.append("high conviction")
            if row.quality_score and row.quality_score >= 8:
                why_parts.append(f"quality={row.quality_score:.1f}")
            if row.market_regime == market_regime:
                why_parts.append(f"same regime={market_regime}")

            weeks_old = row.weeks_old or 0
            if weeks_old < 2:
                why_parts.append("recent")

            retrieved = RetrievedJournal(
                trade_id=row.trade_id,
                symbol=row.symbol,
                setup_type=row.setup_type,
                direction=row.direction,
                pnl_pct=row.pnl_pct,
                entry_text=row.entry_text,
                setup_summary=row.setup_summary,
                outcome=outcome,
                reason=row.reason,
                one_actionable_lesson=row.one_actionable_lesson,
                quality_score=row.quality_score,
                market_regime=row.market_regime,
                is_high_conviction=row.is_high_conviction,
                similarity=row.base_similarity,
                created_at=row.created_at,
                why_selected=", ".join(why_parts) if why_parts else "vector match",
            )
            results.append(retrieved)

        if results:
            top_sim = results[0].similarity
            top_outcome = results[0].outcome
            top_lesson = results[0].one_actionable_lesson or "N/A"
            log.info(
                "Retrieved %d past journals. Top match similarity: %.2f | Outcome: %s | Lesson: %s",
                len(results), top_sim, top_outcome, top_lesson[:50]
            )
            rag_retrieval_count.inc()
            # record averages to Gauges
            rag_avg_similarity.set(sum(r.similarity for r in results) / len(results))
            if any(r.quality_score for r in results):
                avg_qual = sum(r.quality_score for r in results if r.quality_score) / len(results)
                rag_quality_score_avg.set(avg_qual)
        else:
            rag_retrieval_count.inc()
            
        return results

    except Exception as exc:
        log.warning("Hybrid retrieval DB query failed: %s", exc)
        RAG_RETRIEVALS_TOTAL.labels(status="failure").inc()
        return []


def format_retrieved_journals_for_context(
    journals: list[RetrievedJournal],
    include_meta_lessons: bool = False,
) -> str:
    """
    Format retrieved journals for injection into AI context.
    """
    if not journals:
        return ""

    lines = ["=== Past Similar Trades for Learning ==="]

    for i, j in enumerate(journals, 1):
        setup_info = f"{j.symbol} {j.direction} {j.setup_type} | {j.outcome} ({j.pnl_pct:+.2f}%)"
        lesson = j.one_actionable_lesson or j.reason or "See full entry"
        
        lines.append(f"{i}. {setup_info}")
        lines.append(f"   Similarity: {j.similarity:.2f} | {j.why_selected}")
        lines.append(f"   Lesson: {lesson}")
        
        if j.setup_summary:
            lines.append(f"   Setup: {j.setup_summary[:100]}...")
        
        lines.append("")

    if lines[-1] == "":
        lines.pop()

    if include_meta_lessons:
        lines.append("")
        lines.append("=== Meta Lessons Learned So Far ===")
        lines.append("- Prioritize high-conviction setups (8+) in trending markets")
        lines.append("- Same-symbol trades: learn from both wins and losses")
        lines.append("- Quality journals (score >= 8) contain most actionable insights")

    return "\\n".join(lines)
