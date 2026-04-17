"""
yukti/agents/memory.py
Semantic memory for Yukti — stores and retrieves trade journal embeddings via pgvector.
Used to inject relevant past setups into Claude's context (few-shot memory).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voyageai
from sqlalchemy import text as sa_text

from yukti.config import settings

log = logging.getLogger(__name__)

_voyage_client: voyageai.Client | None = None


def _voyage() -> voyageai.Client:
    global _voyage_client
    if _voyage_client is None:
        _voyage_client = voyageai.Client(api_key=settings.voyage_api_key)
    return _voyage_client


async def _embed(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """Async wrapper around the synchronous Voyage AI embedding call."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: _voyage().embed(texts, model="voyage-large-2-instruct", input_type=input_type),
    )
    return result.embeddings


async def embed_journal(journal_text: str) -> list[float]:
    """Generate a single 1024-dim embedding for a journal entry."""
    embeddings = await _embed([journal_text], input_type="document")
    return embeddings[0]


async def store_journal(
    trade_id:      int,
    symbol:        str,
    setup_type:    str,
    direction:     str,
    pnl_pct:       float,
    journal_text:  str,
) -> None:
    """Embed a journal entry and persist it to PostgreSQL with pgvector."""
    from yukti.data.database import get_db
    from yukti.data.models import JournalEntry

    try:
        embedding = await embed_journal(journal_text)
    except Exception as exc:
        log.warning("Embedding failed for trade %d: %s", trade_id, exc)
        embedding = None

    async with get_db() as db:
        db.add(JournalEntry(
            trade_id   = trade_id,
            symbol     = symbol,
            setup_type = setup_type,
            direction  = direction,
            pnl_pct    = pnl_pct,
            entry_text = journal_text,
            embedding  = embedding,
        ))
    log.info("Journal stored for trade %d (%s %s %.2f%%)", trade_id, symbol, direction, pnl_pct)


async def retrieve_similar(
    symbol:     str,
    setup_type: str,
    direction:  str,
    top_k:      int = 3,
) -> str:
    """
    Find the top-k most similar past journal entries using cosine similarity.
    Returns a formatted multi-line string for injection into Claude's context,
    or an empty string if nothing is found.
    """
    from yukti.data.database import get_db

    query_text = f"{symbol} {direction} {setup_type} equity trade NSE"

    try:
        [query_emb] = await _embed([query_text], input_type="query")
    except Exception as exc:
        log.warning("Memory retrieval embedding failed: %s", exc)
        return ""

    sql = sa_text("""
        SELECT entry_text, pnl_pct, setup_type, direction, symbol,
               1 - (embedding <=> :emb ::vector) AS similarity
        FROM   journal_entries
        WHERE  embedding IS NOT NULL
        ORDER  BY embedding <=> :emb ::vector
        LIMIT  :k
    """)

    results: list[str] = []
    try:
        async with get_db() as db:
            rows = (await db.execute(sql, {"emb": str(query_emb), "k": top_k})).fetchall()

        for row in rows:
            outcome = "WIN" if row.pnl_pct > 0 else "LOSS"
            results.append(
                f"  [{outcome} {row.pnl_pct:+.1f}%] "
                f"{row.symbol} {row.direction} {row.setup_type} "
                f"(sim={row.similarity:.2f})\n"
                f"  {row.entry_text}"
            )
    except Exception as exc:
        log.warning("Memory DB query failed: %s", exc)
        return ""

    if not results:
        return ""

    header = f"Past similar setups (top {len(results)}):"
    return header + "\n\n" + "\n\n".join(results)
