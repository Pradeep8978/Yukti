"""
scripts/universe_loader.py
Load the trading watchlist (symbol → DhanHQ security_id) from a CSV file,
or fetch it dynamically from NSE + DhanHQ instruments master.

CSV format:
    symbol,security_id,sector
    RELIANCE,1333,Energy
    HDFCBANK,1232,Banking

Usage:
    uv run python scripts/universe_loader.py --dynamic          # Nifty50 from NSE (recommended)
    uv run python scripts/universe_loader.py --dynamic --index "NIFTY 100"
    uv run python scripts/universe_loader.py --file universe.csv
    uv run python scripts/universe_loader.py --sample           # hardcoded fallback
    uv run python scripts/universe_loader.py --print            # show current universe

The loaded universe is stored in Redis so the running agent can refresh
it without restarting.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
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


# ── Dynamic universe fetch ─────────────────────────────────────────────────────

_NSE_INDEX_URL   = "https://www.nseindia.com/api/equity-stockIndices?index={index}"
_DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
}


async def _fetch_nse_index_symbols(index: str) -> list[dict]:
    """
    Fetch index constituents from NSE API.
    Returns list of {symbol, industry} dicts.
    NSE requires a session cookie — prime with homepage first.
    """
    import httpx
    url = _NSE_INDEX_URL.format(index=index.replace(" ", "%20"))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Prime the session cookie
        await client.get("https://www.nseindia.com", headers=_NSE_HEADERS, timeout=8.0)
        resp = await client.get(url, headers=_NSE_HEADERS, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

    stocks = data.get("data", [])
    # First entry is the index itself — skip it
    return [
        {
            "symbol":   s["symbol"].strip().upper(),
            "industry": s.get("industry", "Unknown").strip(),
        }
        for s in stocks
        if s.get("symbol") and s.get("symbol") != index
    ]


async def _fetch_dhan_security_ids() -> dict[str, str]:
    """
    Download DhanHQ instruments master CSV and return {trading_symbol: security_id}
    for NSE equity segment only.
    """
    import httpx
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(_DHAN_MASTER_URL, timeout=30.0)
        resp.raise_for_status()
        content = resp.text

    reader = csv.DictReader(io.StringIO(content))
    mapping: dict[str, str] = {}
    for row in reader:
        # NSE equity segment rows only
        if (
            row.get("SEM_EXM_EXCH_ID", "").strip().upper() == "NSE"
            and row.get("SEM_SEGMENT", "").strip().upper() == "E"
            and row.get("SEM_INSTRUMENT_NAME", "").strip().upper() == "EQUITY"
        ):
            sym = row.get("SEM_TRADING_SYMBOL", "").strip().upper()
            sid = row.get("SEM_SMST_SECURITY_ID", "").strip()
            if sym and sid:
                mapping[sym] = sid
    return mapping


async def fetch_dynamic_universe(index: str = "NIFTY 50") -> list[dict]:
    """
    Build a trading universe dynamically:
      1. Fetch index constituents from NSE (symbols + sector/industry)
      2. Download DhanHQ instruments master to get security IDs
      3. Cross-reference — skip symbols not found in DhanHQ master

    Falls back to SAMPLE_UNIVERSE if both external calls fail.
    """
    print(f"Fetching {index} constituents from NSE...")
    try:
        nse_stocks = await _fetch_nse_index_symbols(index)
        print(f"  NSE: found {len(nse_stocks)} symbols")
    except Exception as exc:
        print(f"  NSE fetch failed: {exc}")
        print("  Falling back to SAMPLE_UNIVERSE")
        return SAMPLE_UNIVERSE

    print("Downloading DhanHQ instruments master...")
    try:
        dhan_ids = await _fetch_dhan_security_ids()
        print(f"  DhanHQ: loaded {len(dhan_ids):,} NSE equity instruments")
    except Exception as exc:
        print(f"  DhanHQ master fetch failed: {exc}")
        print("  Falling back to SAMPLE_UNIVERSE")
        return SAMPLE_UNIVERSE

    universe: list[dict] = []
    missing: list[str] = []
    for stock in nse_stocks:
        sym = stock["symbol"]
        sid = dhan_ids.get(sym)
        if sid:
            universe.append({
                "symbol":      sym,
                "security_id": sid,
                "sector":      stock["industry"],
            })
        else:
            missing.append(sym)

    if missing:
        print(f"  ⚠️  {len(missing)} symbols not found in DhanHQ master: {', '.join(missing)}")

    print(f"  ✅ Built universe: {len(universe)} symbols")
    return universe


async def build_candidate_pool(
    index: str = "NIFTY 500",
    min_turnover_cr: float = 10.0,
    volatility_band_pct: tuple[float, float] = (1.5, 6.0),
    lookback_days: int = 30,
) -> list[dict]:
    """Build the daily *candidate pool* the dynamic scanner draws from.

    Pipeline:
      1. NSE index constituents ∩ DhanHQ scrip master  (full sectorized list).
      2. For each symbol, fetch ~30 daily candles and compute:
           - 20-day average turnover (Cr).
           - 20-day realized ATR/price as a percentage.
      3. Drop names with turnover < `min_turnover_cr`.
      4. Drop names whose ATR-pct falls outside `volatility_band_pct`
         (default 1.5% – 6.0% — excludes both dead-flat and explosive names).

    Returns: `[{symbol, security_id, sector, turnover_cr, atr_pct}, ...]`.
    """
    base = await fetch_dynamic_universe(index)
    if not base:
        return []

    # Late import to avoid pulling DhanHQ SDK at module import time
    from yukti.execution.dhan_client import dhan
    import pandas as pd

    pool: list[dict] = []
    lo_band, hi_band = volatility_band_pct
    for entry in base:
        sym = entry["symbol"]
        sid = entry["security_id"]
        try:
            raw = await dhan.historical_daily(sid, days=lookback_days, symbol=sym)
            if not raw or len(raw) < 20:
                continue
            df = pd.DataFrame(raw).astype(
                {c: float for c in ("open", "high", "low", "close", "volume") if c in raw[0]}
            )
            if any(c not in df.columns for c in ("high", "low", "close", "volume")):
                continue

            tr = (df["high"] - df["low"]).rolling(20).mean().iloc[-1]
            close = df["close"].iloc[-1]
            atr_pct = float(tr / close * 100) if close > 0 else 0.0

            turnover_cr = float((df["close"] * df["volume"]).rolling(20).mean().iloc[-1] / 1e7)

            if turnover_cr < min_turnover_cr:
                continue
            if not (lo_band <= atr_pct <= hi_band):
                continue

            pool.append({
                "symbol":      sym,
                "security_id": sid,
                "sector":      entry.get("sector", "Unknown"),
                "turnover_cr": round(turnover_cr, 2),
                "atr_pct":     round(atr_pct, 2),
            })
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  candidate_pool: skipping {sym}: {exc}")

    print(f"  ✅ candidate pool: {len(pool)} / {len(base)} symbols passed filters")
    return pool


async def _save_candidate_pool_to_redis(pool: list[dict], ttl_seconds: int = 24 * 3600) -> None:
    import redis.asyncio as aioredis
    from yukti.config import settings

    r = await aioredis.from_url(settings.redis_url, decode_responses=True)
    await r.set("yukti:candidate_pool", json.dumps(pool), ex=ttl_seconds)
    await r.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Yukti universe loader")
    parser.add_argument("--dynamic", action="store_true", help="Fetch universe from NSE + DhanHQ master (recommended)")
    parser.add_argument("--index",   default="NIFTY 50",   help="NSE index to use with --dynamic (default: 'NIFTY 50')")
    parser.add_argument("--candidate-pool", action="store_true",
                        help="Build the daily candidate pool (NIFTY 500 + liquidity + vol band) into yukti:candidate_pool")
    parser.add_argument("--file",    type=Path,             help="Path to universe CSV")
    parser.add_argument("--sample",  action="store_true",   help="Load built-in sample universe (fallback)")
    parser.add_argument("--print",   action="store_true",   help="Print current universe from Redis")
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

    if args.candidate_pool:
        from yukti.config import settings
        index = args.index if args.index != "NIFTY 50" else getattr(settings, "candidate_pool_index", "NIFTY 500")
        band = getattr(settings, "universe_volatility_band_pct", (1.5, 6.0))
        if isinstance(band, list):
            band = tuple(band)
        pool = await build_candidate_pool(
            index=index,
            min_turnover_cr=settings.min_turnover_cr,
            volatility_band_pct=band,
        )
        await _save_candidate_pool_to_redis(pool)
        print(f"✅ candidate pool: {len(pool)} symbols → yukti:candidate_pool (TTL 24h)")
        return

    if args.dynamic:
        universe = await fetch_dynamic_universe(args.index)
        print(f"Dynamic universe loaded: {len(universe)} symbols from {args.index}")
    elif args.sample:
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
