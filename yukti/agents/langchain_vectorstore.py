"""LangChain-compatible PGVector VectorStore adapter for Yukti.

This creates a dedicated `langchain_vectors` table (safe, separate from
`journal_entries`) and provides async/sync methods to add documents and
perform pgvector similarity search. It uses the repo's existing Voyage
embedding helper so embeddings are consistent with `journal_entries`.

Designed to be importable by LangChain code; if `langchain` is present
it will return `langchain.schema.Document` instances where appropriate.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import text as sa_text

from yukti.config import settings
from yukti.data.database import engine
import yukti.agents.memory as memory

log = logging.getLogger(__name__)

try:
    from langchain.schema import Document  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    Document = None


DEFAULT_TABLE = "langchain_vectors"
DEFAULT_DIM = 1024


class LangChainPGVectorStore:
    """Async-backed vector store using Postgres + pgvector.

    Methods:
      - init_table(): create table + index if missing
      - add_texts_async(texts, metadatas): embed and insert rows
      - similarity_search_async(query, k): embed query and return top-k
      - index_journal_entries(): backfill from `journal_entries` (idempotent)
    """

    def __init__(self, table_name: str = DEFAULT_TABLE, dim: int = DEFAULT_DIM):
        self.table_name = table_name
        self.dim = dim

    async def init_table(self) -> None:
        """Create the vectorstore table and (best-effort) ivfflat index."""
        create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.table_name} (
            id SERIAL PRIMARY KEY,
            content TEXT,
            metadata JSONB,
            embedding vector({self.dim})
        )
        """

        # Ensure vector extension and create table/index in a single transaction
        async with engine.begin() as conn:
            await conn.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sa_text(create_table_sql))
            # ivfflat index is optional; if it fails we continue (requires REINDEX tuning)
            try:
                await conn.execute(sa_text(
                    f"CREATE INDEX IF NOT EXISTS {self.table_name}_embedding_idx ON {self.table_name} USING ivfflat (embedding) WITH (lists = 100)"
                ))
            except Exception as exc:  # pragma: no cover - DB index creation may vary
                log.warning("Could not create ivfflat index for %s: %s", self.table_name, exc)

    async def add_texts_async(self, texts: List[str], metadatas: Optional[List[Optional[Dict[str, Any]]]] = None) -> None:
        """Embed `texts` and append them to the vectorstore. Metadata may be None.

        This is async and non-blocking for the current event loop.
        """
        if metadatas is None:
            metadatas = [None] * len(texts)

        embeddings = await memory._embed(texts, input_type="document")

        async with engine.begin() as conn:
            for text, meta, emb in zip(texts, metadatas, embeddings):
                meta_json = json.dumps(meta) if meta is not None else "{}"
                sql = sa_text(
                    f"INSERT INTO {self.table_name} (content, metadata, embedding) VALUES (:content, :metadata::jsonb, :emb::vector)"
                )
                await conn.execute(sql, {"content": text, "metadata": meta_json, "emb": str(emb)})

    async def similarity_search_async(self, query: str, k: int = 3) -> List[Union["Document", dict]]:
        """Embed `query` and return top-k similar documents as Documents or dicts."""
        try:
            [query_emb] = await memory._embed([query], input_type="query")
        except Exception as exc:
            log.warning("VectorStore: embedding failed for query '%s': %s", query, exc)
            return []

        sql = sa_text(f"""
            SELECT content, metadata, 1 - (embedding <=> :emb ::vector) AS similarity
            FROM   {self.table_name}
            WHERE  embedding IS NOT NULL
            ORDER  BY embedding <=> :emb ::vector
            LIMIT  :k
        """)

        async with engine.connect() as conn:
            res = await conn.execute(sql, {"emb": str(query_emb), "k": k})
            rows = res.fetchall()

        results: List[Union["Document", dict]] = []
        for row in rows:
            content = getattr(row, "content", "") or ""
            metadata = getattr(row, "metadata", {}) or {}
            if Document is not None:
                results.append(Document(page_content=content, metadata=metadata))
            else:
                results.append({"page_content": content, "metadata": metadata, "similarity": float(getattr(row, "similarity", 0) or 0)})

        return results

    def add_texts(self, texts: List[str], metadatas: Optional[List[Optional[Dict[str, Any]]]] = None) -> None:
        """Synchronous wrapper for `add_texts_async`."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return asyncio.run(self.add_texts_async(texts, metadatas))
            return loop.run_until_complete(self.add_texts_async(texts, metadatas))
        except RuntimeError:
            return asyncio.run(self.add_texts_async(texts, metadatas))

    def similarity_search(self, query: str, k: int = 3) -> List[Union["Document", dict]]:
        """Synchronous wrapper for `similarity_search_async`."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return asyncio.run(self.similarity_search_async(query, k))
            return loop.run_until_complete(self.similarity_search_async(query, k))
        except RuntimeError:
            return asyncio.run(self.similarity_search_async(query, k))

    async def index_journal_entries(self, batch: int = 500, reindex: bool = True) -> int:
        """Backfill `langchain_vectors` from `journal_entries`.

        This deletes any prior rows labelled with `source: 'journal'` and re-inserts
        all `journal_entries` that contain embeddings. Returns number inserted.
        """
        # Remove prior journal-sourced rows so this is idempotent
        async with engine.begin() as conn:
            if reindex:
                try:
                    await conn.execute(sa_text(f"DELETE FROM {self.table_name} WHERE (metadata->>'source') = 'journal'"))
                except Exception:
                    # ignore if metadata not present or deletion fails
                    pass

            # Fetch journal entries with embeddings
            rows = (await conn.execute(sa_text(
                "SELECT id, trade_id, entry_text, pnl_pct, setup_type, direction, symbol, embedding FROM journal_entries WHERE embedding IS NOT NULL"
            ))).fetchall()

            count = 0
            for row in rows:
                meta = {
                    "source": "journal",
                    "journal_id": getattr(row, "id", None),
                    "trade_id": getattr(row, "trade_id", None),
                    "pnl_pct": float(getattr(row, "pnl_pct", 0)) if getattr(row, "pnl_pct", None) is not None else None,
                    "setup_type": getattr(row, "setup_type", None),
                    "direction": getattr(row, "direction", None),
                    "symbol": getattr(row, "symbol", None),
                }
                content = getattr(row, "entry_text", "") or ""
                emb = getattr(row, "embedding", None)
                if emb is None:
                    continue
                sql = sa_text(
                    f"INSERT INTO {self.table_name} (content, metadata, embedding) VALUES (:content, :metadata::jsonb, :emb::vector)"
                )
                await conn.execute(sql, {"content": content, "metadata": json.dumps(meta), "emb": str(emb)})
                count += 1

        return count


def make_langchain_vectorstore(table_name: str = DEFAULT_TABLE, dim: int = DEFAULT_DIM) -> LangChainPGVectorStore:
    return LangChainPGVectorStore(table_name=table_name, dim=dim)
