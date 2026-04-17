"""
scripts/universe_loader.py
Load the trading watchlist (symbol → DhanHQ security_id) from a CSV file.

CSV format:
    symbol,security_id,sector
    RELIANCE,1333,Energy
    HDFCBANK,1232,Banking

Usage:
    uv run python scripts/universe_loader.py --file universe.csv
    uv run python scripts/universe_loader.py --print   # show current universe

The loaded universe is stored in Redis so the running agent can refresh
it without restarting.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path


SAMPLE_UNIVERSE = [
    # Large-cap Nifty50 — liquid, good for intraday
    {"symbol": "RELIANCE",  "security_id": "1333",  "sector": "Energy"},
    {"symbol": "HDFCBANK",  "security_id": "1232",  "sector": "Banking"},
    {"symbol": "INFY",      "security_id": "1594",  "sector": "IT"},
    {"symbol": "TCS",       "security_id": "11536", "sector": "IT"},
    {"symbol": "ICICIBANK", "security_id": "4963",  "sector": "Banking"},
    {"symbol": "AXISBANK",  "security_id": "5900",  "sector": "Banking"},
    {"symbol": "WIPRO",     "security_id": "3787",  "sector": "IT"},
    {"symbol": "SBIN",      "security_id": "3045",  "sector": "Banking"},
    {"symbol": "BAJFINANCE","security_id": "317",   "sector": "NBFC"},
    {"symbol": "TATAMOTORS","security_id": "3456",  "sector": "Auto"},
    {"symbol": "MARUTI",    "security_id": "10999", "sector": "Auto"},
    {"symbol": "SUNPHARMA", "security_id": "3351",  "sector": "Pharma"},
]


async def _save_to_redis(universe: list[dict]) -> None:
    import redis.asyncio as aioredis
    from yukti.config import settings

    r = await aioredis.from_url(settings.redis_url, decode_responses=True)
    await r.set("yukti:universe", json.dumps(universe))
    await r.aclose()


async def _load_from_file(path: Path) -> list[dict]:
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "symbol":      row["symbol"].strip().upper(),
                "security_id": row["security_id"].strip(),
                "sector":      row.get("sector", "Unknown").strip(),
            })
    return rows


def _as_symbol_map(universe: list[dict]) -> dict[str, str]:
    return {u["symbol"]: u["security_id"] for u in universe}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Yukti universe loader")
    parser.add_argument("--file",    type=Path, help="Path to universe CSV")
    parser.add_argument("--sample",  action="store_true", help="Load built-in sample universe")
    parser.add_argument("--print",   action="store_true", help="Print current universe from Redis")
    args = parser.parse_args()

    if args.print:
        import redis.asyncio as aioredis
        from yukti.config import settings
        r = await aioredis.from_url(settings.redis_url, decode_responses=True)
        raw = await r.get("yukti:universe")
        await r.aclose()
        if not raw:
            print("No universe loaded yet.")
            return
        universe = json.loads(raw)
        print(f"\nCurrent universe ({len(universe)} symbols):\n")
        for u in universe:
            print(f"  {u['symbol']:15s} id={u['security_id']:8s} sector={u['sector']}")
        return

    if args.sample:
        universe = SAMPLE_UNIVERSE
        print(f"Loading sample universe ({len(universe)} symbols)...")
    elif args.file:
        universe = await _load_from_file(args.file)
        print(f"Loading {len(universe)} symbols from {args.file}...")
    else:
        parser.print_help()
        return

    await _save_to_redis(universe)

    # Also write a universe.json for reference
    with open("universe.json", "w") as f:
        json.dump(_as_symbol_map(universe), f, indent=2)

    print(f"✅ Loaded {len(universe)} symbols into Redis and universe.json")
    for u in universe:
        print(f"  {u['symbol']:15s} {u['security_id']:8s} {u['sector']}")


if __name__ == "__main__":
    asyncio.run(main())
