"""
scripts/populate_backtest_data.py

Backfills the backtest environment with REAL DhanHQ historical data:
  - reads universe.json
  - writes yukti:candidate_pool into Redis (so run_backtest*.py can pick it up)
  - pulls daily candles via HistoricalData.historical_daily_data
  - pulls 5-minute candles via HistoricalData.intraday_minute_data (chunked
    to respect Dhan's ~90-day intraday window)
  - also fetches NIFTY index (security_id=13)
  - inserts into postgres `candles` table (interval='D' and '5')

Usage:
    python scripts/populate_backtest_data.py
    python scripts/populate_backtest_data.py --intervals 5 --days 30
    python scripts/populate_backtest_data.py --intervals D --days 365

Note: Dhan returns Unix timestamps; we convert to naive IST datetimes to
match how the live pipeline stores candles. Rows for a given (symbol,
interval) are wiped before re-insert so reruns don't double-count.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("populate")

IST = timezone(timedelta(hours=5, minutes=30))

# Dhan intraday window cap (their API rejects ranges much longer than ~90d)
DHAN_INTRADAY_CHUNK_DAYS = 60


def _ist_naive(unix_ts: float) -> datetime:
    return datetime.fromtimestamp(float(unix_ts), tz=IST).replace(tzinfo=None)


async def _insert_rows(symbol: str, interval: str, rows: list[dict]) -> int:
    from yukti.data.database import get_db
    from yukti.data.models import Candle
    from sqlalchemy import delete

    if not rows:
        return 0

    candles = []
    for r in rows:
        try:
            o = float(r["open"]); h = float(r["high"]); l = float(r["low"]); c = float(r["close"])
            v = float(r.get("volume", 0) or 0)
            if math.isnan(c) or math.isnan(o):
                continue
            candles.append(Candle(
                symbol=symbol,
                interval=interval,
                time=r["time"],
                open=o, high=h, low=l, close=c, volume=v,
            ))
        except (KeyError, TypeError, ValueError):
            continue

    if not candles:
        return 0

    async with get_db() as db:
        await db.execute(delete(Candle).where(Candle.symbol == symbol, Candle.interval == interval))
        db.add_all(candles)
        await db.commit()
    return len(candles)


def _dhan_to_rows(payload: dict) -> list[dict]:
    """Convert Dhan's dict-of-arrays into a list of {time, OHLCV} dicts in IST."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    ts_list = data.get("timestamp", [])
    if not ts_list:
        return []
    rows = []
    for i, ts in enumerate(ts_list):
        try:
            rows.append({
                "time":   _ist_naive(ts),
                "open":   data["open"][i],
                "high":   data["high"][i],
                "low":    data["low"][i],
                "close":  data["close"][i],
                "volume": data.get("volume", [0] * len(ts_list))[i],
            })
        except (IndexError, KeyError):
            continue
    return rows


async def fetch_daily(hd, sec_id: str, exchange: str, instrument: str, start: str, end: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    # Retry on transient failures / throttling; Dhan rate-limits aggressively.
    last_payload = None
    for attempt in range(5):
        res = await loop.run_in_executor(
            None,
            lambda: hd.historical_daily_data(
                security_id=sec_id,
                exchange_segment=exchange,
                instrument_type=instrument,
                from_date=start,
                to_date=end,
            ),
        )
        rows = _dhan_to_rows(res)
        if rows:
            return rows
        last_payload = res
        await asyncio.sleep(0.5 + attempt * 0.5)
    log.debug("daily empty for %s after retries: %s", sec_id,
              (last_payload or {}).get("remarks", "<no remarks>") if isinstance(last_payload, dict) else last_payload)
    return []


async def fetch_5m(hd, sec_id: str, exchange: str, instrument: str, start: str, end: str) -> list[dict]:
    """Pull 5-minute candles, chunked to stay within Dhan's intraday window cap."""
    loop = asyncio.get_event_loop()
    start_d = datetime.fromisoformat(start).date()
    end_d = datetime.fromisoformat(end).date()
    cur = start_d
    all_rows: list[dict] = []
    while cur <= end_d:
        chunk_end = min(cur + timedelta(days=DHAN_INTRADAY_CHUNK_DAYS - 1), end_d)
        try:
            res = await loop.run_in_executor(
                None,
                lambda c=cur, ce=chunk_end: hd.intraday_minute_data(
                    security_id=sec_id,
                    exchange_segment=exchange,
                    instrument_type=instrument,
                    from_date=c.isoformat(),
                    to_date=ce.isoformat(),
                    interval=5,
                ),
            )
            all_rows.extend(_dhan_to_rows(res))
        except Exception as exc:
            log.warning("intraday chunk %s..%s failed for %s: %s", cur, chunk_end, sec_id, exc)
        cur = chunk_end + timedelta(days=1)
        await asyncio.sleep(0.4)  # be polite to the API
    # de-dup by time (in case chunks overlap)
    seen = set()
    dedup: list[dict] = []
    for r in sorted(all_rows, key=lambda x: x["time"]):
        if r["time"] in seen:
            continue
        seen.add(r["time"])
        dedup.append(r)
    return dedup


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--intervals", default="D,5", help="Comma-separated: D and/or 5")
    parser.add_argument("--days", type=int, default=None,
                        help="History window. Default: 400 for daily, 58 for 5m.")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--universe", default="universe.json")
    parser.add_argument("--skip-redis", action="store_true")
    args = parser.parse_args()

    today = datetime.now().date()
    end_date_str = args.end or today.isoformat()

    universe_path = Path(args.universe)
    universe = json.loads(universe_path.read_text())
    symbols = list(universe.keys())
    log.info("Universe: %d symbols", len(symbols))

    # ── Redis: write candidate pool ──────────────────────────────────────
    if not args.skip_redis:
        import redis
        from yukti.config import settings
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        pool = [
            {"symbol": sym, "security_id": sec, "turnover_cr": 0, "atr_pct": 0}
            for sym, sec in universe.items()
        ]
        r.set("yukti:candidate_pool", json.dumps(pool))
        log.info("Redis: wrote yukti:candidate_pool with %d entries", len(pool))

    # ── Dhan client ─────────────────────────────────────────────────────
    from yukti.config import settings
    from dhanhq import DhanContext, HistoricalData
    ctx = DhanContext(client_id=settings.dhan_client_id, access_token=settings.dhan_access_token)
    hd = HistoricalData(ctx)

    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]

    for interval in intervals:
        is_daily = interval in ("D", "1d", "1D", "d")
        db_int = "D" if is_daily else interval
        days = args.days or (400 if is_daily else 58)
        start_d = (today - timedelta(days=days)).isoformat()

        log.info("Fetching %s candles %s → %s for %d symbols + NIFTY",
                 "daily" if is_daily else f"{interval}m", start_d, end_date_str, len(symbols))

        total = 0
        for sym, sec in universe.items():
            try:
                if is_daily:
                    rows = await fetch_daily(hd, sec, "NSE_EQ", "EQUITY", start_d, end_date_str)
                else:
                    rows = await fetch_5m(hd, sec, "NSE_EQ", "EQUITY", start_d, end_date_str)
                inserted = await _insert_rows(sym, db_int, rows)
                total += inserted
                log.info("  %-12s %s candles", sym, inserted)
            except Exception as exc:
                log.warning("  %s [%s]: %s", sym, interval, exc)
            # Pace the requests so Dhan's rate limit doesn't return empty payloads.
            # Empirically ~3 req/sec is safe for historical endpoints.
            await asyncio.sleep(0.35)

        # NIFTY index (security_id=13)
        try:
            if is_daily:
                rows = await fetch_daily(hd, "13", "IDX_I", "INDEX", start_d, end_date_str)
            else:
                rows = await fetch_5m(hd, "13", "IDX_I", "INDEX", start_d, end_date_str)
            inserted = await _insert_rows("NIFTY", db_int, rows)
            total += inserted
            log.info("  %-12s %s candles", "NIFTY", inserted)
        except Exception as exc:
            log.warning("  NIFTY [%s]: %s", interval, exc)

        log.info("Inserted %d candles for interval=%s", total, db_int)


if __name__ == "__main__":
    asyncio.run(main())
