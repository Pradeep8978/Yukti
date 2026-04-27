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
import json
from sqlalchemy import text as sa_text

from yukti.config import settings
from yukti.agents.rag_schemas import JournalReflection, RetrievedTradeContext, RetrievalMetadata, RagSettings
from yukti.metrics import (
    rag_retrieval_count,
    rag_avg_similarity,
    rag_quality_score_avg,
)
from yukti.metrics import rag_retrieved_wins

log = logging.getLogger(__name__)
# Use central metrics exported from yukti.metrics for observability


_voyage_client: voyageai.Client | None = None

from pathlib import Path


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
            f"{i+1}. {ctx.symbol} | {ctx.setup_type or 'unknown'} | {outcome} {ctx.pnl_pct:+.1f}% | sim={ctx.similarity:.2f}\n"
            f"   - Setup summary : {entry_summary}\n"
            f"   - What happened : {ctx.reason or 'See journal entry.'}\n"
            f"   - Key lesson    : {ctx.one_actionable_lesson or '—'}\n"
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

    header = "=== Past Similar Trades for Learning ==="
    body = "\n\n".join(parts)
    if meta:
        body = body + "\n\n" + meta

    # Log concise retrieval info
    if selected:
        top = selected[0]
        top_match = (
            f"{top.symbol} - similarity {top.similarity:.2f} - outcome: {'win' if (top.pnl_pct or 0)>0 else 'loss'}"
            f" - lesson: {top.one_actionable_lesson or top.reason or '—'}"
        )
        log.info("Retrieved %d past trades. Top match: %s", len(selected), top_match)

    return header + "\n\n" + body


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


def _build_why_selected(
    row_sym: str,
    row_setup: str,
    query_sym: str,
    query_setup: str,
    outcome: str,
    pnl_pct: float,
    age_days: int,
    qscore: float,
    base_sim: float,
    recency_days: int,
) -> str:
    """Produce a concise human-readable string explaining why this journal was retrieved."""
    parts: list[str] = []
    if row_sym and row_sym == query_sym:
        parts.append(f"same symbol ({row_sym})")
    if row_setup and row_setup == query_setup:
        parts.append(f"same setup ({row_setup})")
    elif row_setup:
        parts.append(f"similar setup ({row_setup})")
    if outcome == "WIN":
        parts.append("winning trade")
    elif outcome == "LOSS":
        parts.append(f"loss trade (learn what went wrong)")
    if age_days <= recency_days:
        parts.append(f"recent ({age_days}d ago)")
    else:
        parts.append(f"{age_days}d ago")
    if qscore >= 8:
        parts.append(f"high quality ({int(qscore)}/10)")
    parts.append(f"similarity {base_sim:.2f}")
    return ", ".join(parts)


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
    # Load configuration
    config = _get_rag_config()
    max_retrieved = top_k or config.get("max_retrieved", 4)
    min_quality = config.get("min_quality_score", 6.0)
    recency_days = config.get("recency_days", 90)
    outcome_weight = config.get("outcome_weight", 0.15)
    recent_half_life = getattr(settings, "rag_recency_half_life_days", 365)
    max_fetch = getattr(settings, "rag_max_fetch_candidates", max_retrieved * 10)

    from yukti.data.database import get_db

    query_text = f"{symbol} {direction} {setup_type} equity trade NSE"

    # Generate query embedding
    try:
        [query_emb] = await _embed([query_text], input_type="query")
    except Exception as exc:
        log.warning("Hybrid retrieval embedding failed: %s", exc)
        try:
            rag_retrieval_count.inc()
        except Exception:
            pass
        return []

    recency_cutoff = datetime.utcnow() - timedelta(days=recency_days)

    # When market_regime is provided use a UNION SQL pre-filter so regime-matched
    # entries are always represented in the candidate pool, even if they fall
    # outside the global top-N by raw vector distance.  A second branch fetches
    # the broader pool; Python-side de-duplication keeps unique rows only.
    _select_cols = """
            id, trade_id, symbol, setup_type, direction, pnl_pct,
            entry_text, setup_summary, outcome, reason,
            one_actionable_lesson, quality_score, market_regime,
            is_high_conviction, created_at,
            1 - (embedding <=> :emb ::vector) AS base_similarity"""
    _base_where = """embedding IS NOT NULL
          AND (discarded IS NULL OR discarded = FALSE)
          AND created_at >= :recency_cutoff"""

    if market_regime:
        # Half of the budget reserved for regime-matched entries (guaranteed slot),
        # the other half for the global nearest neighbours.
        limit_regime = max(8, max_fetch // 2)
        limit_all = max_fetch
        sql = sa_text(f"""
            (SELECT {_select_cols}
             FROM journal_entries
             WHERE {_base_where}
               AND market_regime = :market_regime
             ORDER BY embedding <=> :emb ::vector
             LIMIT :limit_regime)
            UNION ALL
            (SELECT {_select_cols}
             FROM journal_entries
             WHERE {_base_where}
             ORDER BY embedding <=> :emb ::vector
             LIMIT :limit_all)
        """)
        sql_params = {
            "emb": str(query_emb),
            "recency_cutoff": recency_cutoff,
            "market_regime": market_regime,
            "limit_regime": limit_regime,
            "limit_all": limit_all,
        }
        log.debug(
            "RAG hybrid SQL pre-filter: market_regime=%s (regime_limit=%d, all_limit=%d)",
            market_regime, limit_regime, limit_all,
        )
    else:
        sql = sa_text(f"""
            SELECT {_select_cols}
            FROM journal_entries
            WHERE {_base_where}
            ORDER BY embedding <=> :emb ::vector
            LIMIT :limit
        """)
        sql_params = {
            "emb": str(query_emb),
            "recency_cutoff": recency_cutoff,
            "limit": max_fetch,
        }

    try:
        async with get_db() as db:
            raw_rows = (await db.execute(sql, sql_params)).fetchall()

        # De-duplicate by journal id (UNION ALL may return the same row twice
        # when a regime-matched entry also appears in the global top-N).
        seen_ids: set[int] = set()
        rows = []
        for r in raw_rows:
            rid = getattr(r, "id", None)
            if rid is None or rid not in seen_ids:
                rows.append(r)
                if rid is not None:
                    seen_ids.add(rid)

        now = datetime.utcnow()
        candidates: list[tuple[float, float, Any]] = []  # (final_score, base_sim, row)

        for row in rows:
            try:
                base_sim = float(getattr(row, "base_similarity", 0.0) or 0.0)
                qscore = float(getattr(row, "quality_score", 0.0) or 0.0)
                created_at = getattr(row, "created_at", None) or now
                age_days = max(0.0, (now - created_at).days)

                # recency decay via half-life
                decay = 0.5 ** (age_days / max(1.0, recent_half_life))

                # Outcome multiplier
                pnl = float(getattr(row, "pnl_pct", 0.0) or 0.0)
                is_win = pnl > 0
                outcome_mul = 1.0 + (outcome_weight if is_win else -outcome_weight * 0.5)

                # Symbol / regime boost
                symbol_bonus = 1.25 if (getattr(row, "symbol", "") or "") == symbol else 1.0
                regime_bonus = 1.10 if (market_regime and (getattr(row, "market_regime", None) == market_regime)) else 1.0

                # Quality multiplier (0.5..1.0+)
                quality_mul = max(0.5, (qscore / 10.0) if qscore > 0 else 0.5)

                final_score = base_sim * outcome_mul * symbol_bonus * regime_bonus * decay * quality_mul

                # store candidate with metadata for richer explanation and MMR
                candidates.append((final_score, base_sim, row, {
                    "outcome_mul": outcome_mul,
                    "decay": decay,
                    "quality_mul": quality_mul,
                    "symbol_bonus": symbol_bonus,
                    "regime_bonus": regime_bonus,
                }))
            except Exception:
                continue

        if not candidates:
            try:
                rag_retrieval_count.inc()
            except Exception:
                pass
            return []

        # sort by final_score desc
        candidates.sort(key=lambda t: t[0], reverse=True)

        # Advanced diversity selection using MMR (maximal marginal relevance) if embeddings available
        lambda_div = getattr(settings, "rag_diversity_lambda", 0.7)

        # Prepare candidate texts for embedding (short summary + lesson + reason)
        candidate_rows = [c[2] for c in candidates]
        candidate_scores = [c[0] for c in candidates]
        candidate_base_sims = [c[1] for c in candidates]
        candidate_meta = [c[3] for c in candidates]

        candidate_texts = []
        for row in candidate_rows:
            parts = []
            if getattr(row, "setup_summary", None):
                parts.append(str(getattr(row, "setup_summary")))
            if getattr(row, "one_actionable_lesson", None):
                parts.append(str(getattr(row, "one_actionable_lesson")))
            if getattr(row, "reason", None):
                parts.append(str(getattr(row, "reason")))
            if not parts:
                parts.append(str(getattr(row, "entry_text", ""))[:400])
            candidate_texts.append(" \n ".join(parts)[:1500])

        emboks = None
        try:
            if candidate_texts:
                emboks = await _embed(candidate_texts, input_type="document")
        except Exception:
            emboks = None

        def cos_sim(a, b):
            try:
                da = math.sqrt(sum(x * x for x in a))
                db = math.sqrt(sum(x * x for x in b))
                if da == 0 or db == 0:
                    return 0.0
                return sum(x * y for x, y in zip(a, b)) / (da * db)
            except Exception:
                return 0.0

        selected_indices: list[int] = []

        if emboks is not None and len(emboks) == len(candidate_rows):
            # Normalize relevance to [0,1]
            max_score = max(candidate_scores) if candidate_scores else 1.0
            rel = [s / max_score if max_score > 0 else 0.0 for s in candidate_scores]

            # MMR selection
            remaining = set(range(len(candidate_rows)))
            # pick highest relevance first
            first = max(remaining, key=lambda i: rel[i])
            selected_indices.append(first)
            remaining.remove(first)

            while len(selected_indices) < max_retrieved and remaining:
                best_i = None
                best_score = None
                for i in list(remaining):
                    diversity_penalty = 0.0
                    if selected_indices:
                        diversity_penalty = max(cos_sim(emboks[i], emboks[j]) for j in selected_indices)
                    mmr_score = lambda_div * rel[i] - (1 - lambda_div) * diversity_penalty
                    if best_score is None or mmr_score > best_score:
                        best_score = mmr_score
                        best_i = i
                if best_i is None:
                    break
                selected_indices.append(best_i)
                remaining.remove(best_i)

            # Build selected list from indices
            selected = []
            outcome_counts = {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0}
            for idx in selected_indices[:max_retrieved]:
                row = candidate_rows[idx]
                base_sim = candidate_base_sims[idx]
                score = candidate_scores[idx]
                meta = candidate_meta[idx]
                outcome = (getattr(row, "outcome", None) or ("WIN" if (getattr(row, "pnl_pct", 0) or 0) > 0 else "LOSS"))
                # Build human-readable retrieval reason
                _why: list[str] = []
                _row_sym = getattr(row, "symbol", "") or ""
                _row_setup = getattr(row, "setup_type", "") or ""
                _age = max(0, (now - (getattr(row, "created_at", now) or now)).days)
                _qsc = float(getattr(row, "quality_score", 0) or 0)
                if meta.get("symbol_bonus", 1.0) > 1.0:
                    _why.append(f"same symbol ({_row_sym})")
                if _row_setup and _row_setup == setup_type:
                    _why.append(f"same setup ({_row_setup})")
                elif _row_setup:
                    _why.append(f"similar setup ({_row_setup})")
                if meta.get("regime_bonus", 1.0) > 1.0:
                    _why.append("same market regime")
                if outcome in ("WIN",) and meta.get("outcome_mul", 1.0) > 1.0:
                    _why.append("winning trade (boosted)")
                if _age <= recency_days:
                    _why.append(f"recent ({_age}d ago)")
                else:
                    _why.append(f"{_age}d ago")
                if _qsc >= 8:
                    _why.append(f"high quality ({int(_qsc)}/10)")
                _why.append(f"similarity {base_sim:.2f}")
                r = RetrievedJournal(
                    trade_id = getattr(row, "trade_id", None),
                    symbol = getattr(row, "symbol", None),
                    setup_type = getattr(row, "setup_type", None),
                    direction = getattr(row, "direction", None),
                    pnl_pct = float(getattr(row, "pnl_pct", 0.0) or 0.0),
                    entry_text = getattr(row, "entry_text", "") or "",
                    setup_summary = getattr(row, "setup_summary", None),
                    outcome = outcome,
                    reason = getattr(row, "reason", None),
                    one_actionable_lesson = getattr(row, "one_actionable_lesson", None) or getattr(row, "key_lesson", None),
                    quality_score = getattr(row, "quality_score", None),
                    market_regime = getattr(row, "market_regime", None),
                    is_high_conviction = bool(getattr(row, "is_high_conviction", False)),
                    similarity = float(base_sim),
                    created_at = getattr(row, "created_at", now) or now,
                    why_selected = ", ".join(_why),
                )
                selected.append(r)

        else:
            # Fallback greedy with simple diversity cap (legacy behavior)
            selected = []
            outcome_counts = {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0}
            max_same_outcome = min(2, max_retrieved)
            for score, base_sim, row, meta in candidates:
                if len(selected) >= max_retrieved:
                    break
                outcome = (getattr(row, "outcome", None) or ("WIN" if (getattr(row, "pnl_pct", 0) or 0) > 0 else "LOSS"))
                if outcome_counts.get(outcome, 0) >= max_same_outcome:
                    if not (getattr(row, "is_high_conviction", False) or (getattr(row, "quality_score", 0) or 0) >= 8):
                        continue
                outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
                r = RetrievedJournal(
                    trade_id = getattr(row, "trade_id", None),
                    symbol = getattr(row, "symbol", None),
                    setup_type = getattr(row, "setup_type", None),
                    direction = getattr(row, "direction", None),
                    pnl_pct = float(getattr(row, "pnl_pct", 0.0) or 0.0),
                    entry_text = getattr(row, "entry_text", "") or "",
                    setup_summary = getattr(row, "setup_summary", None),
                    outcome = outcome,
                    reason = getattr(row, "reason", None),
                    one_actionable_lesson = getattr(row, "one_actionable_lesson", None) or getattr(row, "key_lesson", None),
                    quality_score = getattr(row, "quality_score", None),
                    market_regime = getattr(row, "market_regime", None),
                    is_high_conviction = bool(getattr(row, "is_high_conviction", False)),
                    similarity = float(base_sim),
                    created_at = getattr(row, "created_at", now) or now,
                    why_selected = _build_why_selected(
                        row_sym=getattr(row, "symbol", "") or "",
                        row_setup=getattr(row, "setup_type", "") or "",
                        query_sym=symbol,
                        query_setup=setup_type,
                        outcome=outcome,
                        pnl_pct=float(getattr(row, "pnl_pct", 0.0) or 0.0),
                        age_days=max(0, (now - (getattr(row, "created_at", now) or now)).days),
                        qscore=float(getattr(row, "quality_score", 0) or 0),
                        base_sim=float(base_sim),
                        recency_days=recency_days,
                    ),
                )
                selected.append(r)

        # Metrics and logging
        try:
            rag_retrieval_count.inc()
            if selected:
                rag_avg_similarity.set(sum(r.similarity for r in selected) / len(selected))
                qvals = [r.quality_score for r in selected if r.quality_score is not None]
                if qvals:
                    rag_quality_score_avg.set(sum(qvals) / len(qvals))
                wins = sum(1 for r in selected if (r.outcome or '').upper() == 'WIN' or (r.pnl_pct or 0) > 0)
                try:
                    rag_retrieved_wins.inc(wins)
                except Exception:
                    pass
                # structured logging
                try:
                    log.info(json.dumps({
                        "event": "rag_retrieval",
                        "symbol": symbol,
                        "setup_type": setup_type,
                        "retrieved_count": len(selected),
                        "avg_similarity": sum(r.similarity for r in selected) / len(selected),
                        "avg_quality": (sum(qvals) / len(qvals)) if qvals else None,
                        "wins": wins,
                        "top_match_trade_id": selected[0].trade_id if selected else None,
                    }))
                except Exception:
                    pass

                # Human-readable per-entry log (one line each) for debugging
                for i, r in enumerate(selected, 1):
                    log.info(
                        "RAG[%d/%d] %s %s | sim=%.2f q=%s | %s | Why: %s",
                        i, len(selected),
                        r.symbol or "?",
                        r.outcome or "?",
                        r.similarity,
                        f"{r.quality_score:.0f}" if r.quality_score is not None else "?",
                        f"pnl={r.pnl_pct:+.1f}%" if r.pnl_pct is not None else "",
                        r.why_selected or "vector match",
                    )
        except Exception:
            pass

        return selected

    except Exception as exc:
        log.warning("Hybrid retrieval DB query failed: %s", exc)
        try:
            rag_retrieval_count.inc()
        except Exception:
            pass
        return []


async def retrieve_similar_trades(
    symbol: str,
    setup_type: str,
    direction: str,
    market_regime: Optional[str] = None,
    top_k: int = 4,
) -> list[RetrievedJournal]:
    """
    Public wrapper for hybrid retrieval: returns top-k `RetrievedJournal` entries
    with full metadata, metrics, and structured logs.
    """
    results = await retrieve_similar_hybrid(symbol, setup_type, direction, market_regime=market_regime, top_k=top_k)
    # Ensure we only return up to top_k
    if results and len(results) > top_k:
        results = results[:top_k]

    # Emit a retrieval count metric (already set inside hybrid, but be explicit)
    try:
        rag_retrieval_count.inc()
    except Exception:
        pass

    # Additional structured logging
    try:
        log.info(json.dumps({
            "event": "retrieve_similar_trades",
            "symbol": symbol,
            "setup_type": setup_type,
            "direction": direction,
            "returned": len(results),
            "top_sim": results[0].similarity if results else None,
        }))
    except Exception:
        pass

    return results


def format_retrieved_journals_for_context(
    journals: list[RetrievedJournal],
    include_meta_lessons: bool = False,
) -> str:
    """Format retrieved journals for injection into Arjun's AI context.

    Produces a numbered, educational block that the LLM can reason over:
      - Setup summary (what happened)
      - Outcome + P&L
      - Key lesson extracted from the trade
      - Similarity score (0-1)
      - Why this trade was retrieved (human-readable)
    """
    if not journals:
        return ""
    lines = ["=== Past Similar Trades for Learning ==="]

    for i, j in enumerate(journals, 1):
        direction_str = f" {j.direction}" if j.direction else ""
        setup_label = j.setup_type or "unknown"
        header = f"{i}. {j.symbol}{direction_str} | {setup_label} | {j.outcome} {j.pnl_pct:+.1f}%"
        lines.append(header)

        # Setup narrative (prefer setup_summary, fall back to first 200 chars of entry_text)
        setup_detail = (j.setup_summary or j.entry_text or "").strip()
        if len(setup_detail) > 220:
            setup_detail = setup_detail[:220].rsplit(" ", 1)[0] + "…"
        if setup_detail:
            lines.append(f"   Setup   : {setup_detail}")

        # What happened
        outcome_detail = (j.reason or "").strip()
        if outcome_detail:
            lines.append(f"   Outcome : {outcome_detail}")

        lesson = (j.one_actionable_lesson or "—").strip()
        lines.append(f"   Lesson  : {lesson}")
        lines.append(f"   Sim={j.similarity:.2f} | Why retrieved: {j.why_selected or 'vector match'}")
        lines.append("")

    # Remove trailing blank
    if lines and lines[-1] == "":
        lines.pop()

    # Include meta-lessons cache if requested (reads data/meta_lessons.json produced by the daily job)
    if include_meta_lessons:
        try:
            meta_path = Path("data") / "meta_lessons.json"
            if meta_path.exists():
                with open(meta_path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                lessons = payload.get("lessons", [])
                if lessons:
                    lines.append("")
                    lines.append("=== Recurring Lessons Across All Past Trades ===")
                    for item in lessons[:5]:  # cap at 5 to keep tokens low
                        cnt = item.get("count", 1)
                        lines.append(f"- {item.get('lesson')} (seen {cnt}x)")
        except Exception:
            pass  # Skip silently — meta_lessons.json may not exist yet

    return "\n".join(lines)
