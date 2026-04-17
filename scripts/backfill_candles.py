"""
scripts/backfill_candles.py
Backfill historical OHLCV candles from DhanHQ into PostgreSQL.
Required before any meaningful backtest.

Usage:
    # Backfill all watchlist symbols for last 2 years
    uv run python scripts/backfill_candles.py --days 730

    # Backfill specific symbols
    uv run python scripts/backfill_candles.py --symbols RELIANCE,TCS --days 365

    # Backfill Nifty only (index security_id = 13)
    uv run python scripts/backfill_candles.py --nifty --days 730

    # Check existing coverage
    uv run python scripts/backfill_candles.py --check
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")


async def backfill_symbol(
    symbol: str,
    security_id: str,
    interval: str,
    days: int,
) -> int:
    """Backfill `days` of `interval`-minute candles for one symbol. Returns rows inserted."""
    from yukti.execution.dhan_client import DhanClient
    from yukti.data.database import get_db
    from yukti.data.models import Candle

    dhan = DhanClient()

    end   = datetime.now()
    start = end - timedelta(days=days)

    # DhanHQ limit: typically 30 days per request for intraday
    # Chunk the backfill into 20-day windows
    chunk_days = 20
    total_inserted = 0

    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=chunk_days), end)
        start_str = cur.strftime("%Y-%m-%d")
        end_str   = chunk_end.strftime("%Y-%m-%d")

        log.info("  %s: fetching %s → %s", symbol, start_str, end_str)

        try:
            raw = await dhan.get_candles(security_id, interval, start_str, end_str)
        except Exception as exc:
            log.warning("  %s: fetch failed for %s: %s", symbol, start_str, exc)
            cur = chunk_end
            continue

        if not raw:
            cur = chunk_end
            continue

        rows_to_insert = []
        for row in raw:
            try:
                rows_to_insert.append(Candle(
                    symbol   = symbol,
                    interval = interval,
                    time     = datetime.fromisoformat(row.get("time", "")) if isinstance(row.get("time"), str) else row.get("time"),
                    open     = float(row.get("open",   0)),
                    high     = float(row.get("high",   0)),
                    low      = float(row.get("low",    0)),
                    close    = float(row.get("close",  0)),
                    volume   = float(row.get("volume", 0)),
                ))
            except Exception as exc:
                log.debug("  skip malformed row: %s", exc)
                continue

        if rows_to_insert:
            async with get_db() as db:
                db.add_all(rows_to_insert)
                await db.commit()
            total_inserted += len(rows_to_insert)

        cur = chunk_end
        await asyncio.sleep(1)   # be gentle on the API

    log.info("  %s: inserted %d candles", symbol, total_inserted)
    return total_inserted


async def check_coverage() -> None:
    """Report current candle coverage per symbol."""
    from sqlalchemy import select, func
    from yukti.data.database import get_db
    from yukti.data.models import Candle

    async with get_db() as db:
        result = await db.execute(
            select(
                Candle.symbol,
                Candle.interval,
                func.min(Candle.time).label("from_time"),
                func.max(Candle.time).label("to_time"),
                func.count(Candle.id).label("rows"),
            )
            .group_by(Candle.symbol, Candle.interval)
        )
        rows = result.all()

    if not rows:
        print("\nNo candles in database. Run backfill first.\n")
        return

    print("\n╔══ CANDLE COVERAGE ══════════════════════════════════════════╗")
    print(f"  {'Symbol':<15} {'Int':<5} {'From':<20} {'To':<20} {'Rows':>8}")
    print("  " + "─" * 65)
    for r in sorted(rows, key=lambda x: x.symbol):
        print(f"  {r.symbol:<15} {r.interval:<5} "
              f"{str(r.from_time)[:19]:<20} {str(r.to_time)[:19]:<20} {r.rows:>8}")
    print("╚══════════════════════════════════════════════════════════════╝\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Yukti candle backfiller")
    parser.add_argument("--days",     type=int, default=365, help="Days of history to fetch")
    parser.add_argument("--interval", default="5", help="Minute interval: 1, 5, 15, 60, D")
    parser.add_argument("--symbols",  default=None,
                        help="Comma-separated symbols (default: all from universe.json)")
    parser.add_argument("--nifty",    action="store_true", help="Also backfill NIFTY (id=13)")
    parser.add_argument("--check",    action="store_true", help="Just show coverage, no backfill")
    args = parser.parse_args()

    if args.check:
        await check_coverage()
        return

    # Load symbol list
    if args.symbols:
        symbol_list = {s.strip().upper(): "" for s in args.symbols.split(",")}
        # Need security_ids — try loading from universe.json
        uni_path = Path("universe.json")
        if uni_path.exists():
            universe = json.loads(uni_path.read_text())
            symbol_list = {s: universe[s] for s in symbol_list if s in universe}
    else:
        uni_path = Path("universe.json")
        if not uni_path.exists():
            log.error("universe.json not found — run scripts/universe_loader.py --sample first")
            return
        symbol_list = json.loads(uni_path.read_text())

    if args.nifty:
        symbol_list["NIFTY"] = "13"

    log.info("Backfilling %d symbols × %d days (%s-minute candles)",
             len(symbol_list), args.days, args.interval)

    total = 0
    for symbol, security_id in symbol_list.items():
        if not security_id:
            log.warning("Skipping %s — no security_id", symbol)
            continue
        count = await backfill_symbol(symbol, security_id, args.interval, args.days)
        total += count

    log.info("Backfill complete — %d total candles inserted", total)
    await check_coverage()


if __name__ == "__main__":
    asyncio.run(main())
