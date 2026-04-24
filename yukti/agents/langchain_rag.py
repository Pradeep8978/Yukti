"""LangChain RAG adapter for Yukti using Postgres + pgvector.

This module provides a thin LangChain-style retriever backed by the
existing `journal_entries` table which stores `embedding` (pgvector).

It uses `voyageai` (already used in the repo) to embed the query and
executes the same fast pgvector SQL used elsewhere in the codebase.

If `langchain` is installed, returned documents are `langchain.schema.Document`
instances; otherwise a simple dict with `page_content` and `metadata` is returned.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Union

from sqlalchemy import text as sa_text

from yukti.data.database import get_db
from yukti import agents as agents_pkg
import yukti.agents.memory as memory

log = logging.getLogger(__name__)


try:
    from langchain.schema import Document  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    Document = None


class PGVectorLangChainRetriever:
    """Retriever that returns similar `journal_entries` as LangChain Documents.

    Usage:
        retriever = PGVectorLangChainRetriever(top_k=3)
        docs = await retriever.aget_relevant_documents("RELIANCE long pullback")
"""

    def __init__(self, top_k: int = 3):
        self.top_k = top_k
        # Prefer the LangChain vectorstore adapter if available
        try:
            from yukti.agents.langchain_vectorstore import make_langchain_vectorstore
            self._vectorstore = make_langchain_vectorstore()
        except Exception:
            self._vectorstore = None

    async def aget_relevant_documents(self, query: str, k: Optional[int] = None) -> List[Union["Document", dict]]:
        """Async retrieval: embed `query` and return top-k similar journal entries.

        Returns a list of `langchain.schema.Document` when LangChain is available,
        otherwise returns a list of dicts with `page_content` and `metadata`.
        """
        top_k = k or self.top_k

        # If a dedicated LangChain-compatible vectorstore is available, use it
        if self._vectorstore is not None:
            try:
                await self._vectorstore.init_table()
                return await self._vectorstore.similarity_search_async(query, top_k)
            except Exception:
                # fall back to direct DB query on any failure
                pass

        try:
            [query_emb] = await memory._embed([query], input_type="query")
        except Exception as exc:
            log.warning("RAG: embedding failed for query '%s': %s", query, exc)
            return []

        sql = sa_text("""
            SELECT id, trade_id, entry_text, pnl_pct, setup_type, direction, symbol,
                   1 - (embedding <=> :emb ::vector) AS similarity
            FROM   journal_entries
            WHERE  embedding IS NOT NULL
            ORDER  BY embedding <=> :emb ::vector
            LIMIT  :k
        """)

        try:
            async with get_db() as db:
                rows = (await db.execute(sql, {"emb": str(query_emb), "k": top_k})).fetchall()
        except Exception as exc:
            log.warning("RAG DB query failed: %s", exc)
            return []

        results: List[Union["Document", dict]] = []
        for row in rows:
            metadata = {
                "journal_id": getattr(row, "id", None),
                "trade_id": getattr(row, "trade_id", None),
                "pnl_pct": float(getattr(row, "pnl_pct", 0)) if getattr(row, "pnl_pct", None) is not None else None,
                "setup_type": getattr(row, "setup_type", None),
                "direction": getattr(row, "direction", None),
                "symbol": getattr(row, "symbol", None),
                "similarity": float(getattr(row, "similarity", 0)) if getattr(row, "similarity", None) is not None else None,
            }
            content = getattr(row, "entry_text", "") or ""
            if Document is not None:
                results.append(Document(page_content=content, metadata=metadata))
            else:
                results.append({"page_content": content, "metadata": metadata})

        return results

    def get_relevant_documents(self, query: str, k: Optional[int] = None) -> List[Union["Document", dict]]:
        """Synchronous wrapper for `aget_relevant_documents`.

        Note: in an already-running event loop (e.g. FastAPI) prefer calling
        `aget_relevant_documents` directly to avoid loop conflicts.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If loop is running, schedule and wait using `asyncio.run` which
                # creates a fresh loop. This avoids `RuntimeError` in most cases.
                return asyncio.run(self.aget_relevant_documents(query, k))
            return loop.run_until_complete(self.aget_relevant_documents(query, k))
        except RuntimeError:
            return asyncio.run(self.aget_relevant_documents(query, k))


def make_langchain_retriever(top_k: int = 3) -> PGVectorLangChainRetriever:
    return PGVectorLangChainRetriever(top_k=top_k)
