"""Test script for PGVectorLangChainRetriever.

Requires a configured Postgres with `journal_entries` and embeddings.
Run locally with `.env` containing `POSTGRES_URL` and `VOYAGE_API_KEY`.
"""
from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    parser_args = sys.argv[1:]
    query = " ".join(parser_args) if parser_args else "RELIANCE long pullback"

    try:
        from yukti.agents.langchain_rag import make_langchain_retriever
    except Exception as exc:
        print("Failed to import RAG retriever:", exc, file=sys.stderr)
        return 2

    retriever = make_langchain_retriever(top_k=3)
    docs = await retriever.aget_relevant_documents(query)

    print(f"Top {len(docs)} documents for query: {query}\n")
    for i, d in enumerate(docs, 1):
        if hasattr(d, "page_content"):
            content = d.page_content
            meta = d.metadata
        else:
            content = d.get("page_content")
            meta = d.get("metadata")
        print(f"--- RESULT #{i} (sim={meta.get('similarity')})")
        print(content[:1000])
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
