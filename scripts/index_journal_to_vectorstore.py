"""Index existing `journal_entries` into the LangChain PGVector table.

This script will create the `langchain_vectors` table if missing and then
copy all `journal_entries` that already have embeddings into it. It is safe
to re-run; previous journal-sourced rows are removed first to avoid duplicates.
"""
from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    try:
        from yukti.agents.langchain_vectorstore import make_langchain_vectorstore
    except Exception as exc:
        print("Failed to import LangChain vectorstore adapter:", exc, file=sys.stderr)
        return 2

    store = make_langchain_vectorstore()
    await store.init_table()
    n = await store.index_journal_entries()
    print(f"Indexed {n} journal entries into the vectorstore.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
