"""
yukti/agents/memory.py
Advanced hybrid retrieval and journal storage for Yukti.

Features:
- Structured `store_journal` accepting `JournalReflection`.
- `retrieve_similar` implements hybrid scoring: vector similarity + metadata filters,
  outcome-weighting, recency decay, and simple diversity heuristic.
- Emission of Prometheus metrics for observability.
"""
from __future__ import annotations

import asyncio
import logging
import math
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

# Prometheus metrics for RAG observability
try:
    from prometheus_client import Counter, Histogram
    
    RAG_RETRIEVALS_TOTAL = Counter(
        "rag_retrievals_total",
        "Total number of RAG retrieval attempts",
        ["status"],  # success, failure, fallback
    )
    RAG_AVG_SIMILARITY = Histogram(
        "rag_avg_similarity",
        "Average similarity score of retrieved journals",
        buckets=[0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0],
    )
    RAG_AVG_QUALITY = Histogram(
        "rag_avg_quality_score",
        "Average quality score of retrieved journals",
        buckets=[0, 2, 4, 6, 8, 10],
    )
    RAG_RETRIEVAL_LATENCY = Histogram(
        "rag_retrieval_latency_seconds",
        "Time taken for RAG retrieval",
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0],
    )
except ImportError:
    # Prometheus not available — create no-op metrics
    class _NoOpCounter:
        def labels(self, **kwargs): return self
        def inc(self, n=1): pass
    
    class _NoOpHistogram:
        def labels(self, **kwargs): return self
        def observe(self, n): pass
    
    RAG_RETRIEVALS_TOTAL = _NoOpCounter()
    RAG_AVG_SIMILARITY = _NoOpHistogram()
    RAG_AVG_QUALITY = _NoOpHistogram()
    RAG_RETRIEVAL_LATENCY = _NoOpHistogram()


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
        refl = JournalReflection(entry_text=journal, quality_score=0, key_lesson="", created_at=datetime.utcnow())
    else:
        refl = journal

    try:
        embedding = await embed_journal(refl.entry_text)
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
            entry_text=refl.entry_text,
            embedding=embedding,
            quality_score=refl.quality_score,
            key_lesson=refl.key_lesson,
            market_regime=refl.market_regime,
            outcome_reason=refl.outcome_reason,
            one_actionable_lesson=refl.one_actionable_lesson,
            outcome=outcome,
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
                SELECT id, trade_id, entry_text, pnl_pct, setup_type, direction, symbol, quality_score, key_lesson, outcome_reason, created_at
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
            parts.append(f"  [{outcome} {row.pnl_pct:+.1f}%] {row.symbol} {row.direction} {row.setup_type}\n  {row.entry_text}")
        header = f"Past similar setups (fallback {len(parts)}):"
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
            key_lesson=getattr(row, "key_lesson", None) or getattr(row, "one_actionable_lesson", None),
            outcome_reason=getattr(row, "outcome_reason", None) or getattr(row, "reason", None),
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
        entry_summary = (ctx.key_lesson or ctx.outcome_reason or "").strip() or "(no summary)"
        why = ctx.retrieval_reason or ""
        
        parts.append(
            f"{i+1}. {ctx.symbol} | {ctx.setup_type or 'unknown'} | {outcome} {ctx.pnl_pct:+.1f}% | sim={ctx.similarity:.2f}\n"
            f"   - Setup summary : {entry_summary}\n"
            f"   - What happened : {ctx.outcome_reason or 'See journal entry.'}\n"
            f"   - Key lesson    : {ctx.key_lesson or '—'}\n"
            f"   - Retrieved because: {why}"
        )

    # Meta lessons: simple frequency of key_lesson in recent journals
    meta = ""
    try:
        recent_cutoff = datetime.utcnow() - timedelta(days=cfg.recency_days)
        sql_meta = sa_text("""
            SELECT key_lesson, COUNT(*) as cnt
            FROM journal_entries
            WHERE key_lesson IS NOT NULL AND quality_score >= :min_q AND created_at >= :cutoff
            GROUP BY key_lesson
            ORDER BY cnt DESC
            LIMIT 3
        """)
        async with get_db() as db:
            rows_meta = (await db.execute(sql_meta, {"min_q": cfg.min_quality_score, "cutoff": recent_cutoff})).fetchall()
        if rows_meta:
            lessons = [f"{r.key_lesson} ({r.cnt})" for r in rows_meta]
            meta = "Meta Lessons Learned: " + ", ".join(lessons)
    except Exception:
        meta = ""

    header = f"Past Similar Trades (top {len(selected)}):"
    body = "\n\n".join(parts)
    if meta:
        body = body + "\n\n" + meta

    # Log concise retrieval info
    if selected:
        top = selected[0]
        top_match = f"{top.symbol} - similarity {top.similarity:.2f} - outcome: {'win' if (top.pnl_pct or 0)>0 else 'loss'} - lesson: {top.key_lesson or '—'}"
        log.info("Retrieved %d past trades. Top match: %s", len(selected), top_match)

    return header + "\n\n" + body
