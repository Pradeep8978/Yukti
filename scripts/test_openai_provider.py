"""Small script to call the OpenAI provider and print the decision + metadata.

Usage:
    python scripts/test_openai_provider.py --symbol RELIANCE

Requires `OPENAI_API_KEY` in environment or `.env`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys


async def main() -> int:
    parser = argparse.ArgumentParser(description="Test OpenAI provider")
    parser.add_argument("--symbol", default="RELIANCE")
    args = parser.parse_args()

    try:
        from yukti.agents.openai_provider import OpenAIProvider
    except Exception as exc:
        print("OpenAI provider not available:", exc, file=sys.stderr)
        return 2

    provider = OpenAIProvider()
    ctx = f"STOCK: {args.symbol} ══\nPlease decide TRADE or SKIP and return valid JSON matching Arjun schema."
    decision, meta = await provider.call(ctx)
    print("Decision:\n", decision.json(indent=2))
    print("Meta:\n", meta.log_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
