"""
scripts/run_backtest.py
CLI runner for the Yukti backtest engine.
Loads historical candles from PostgreSQL (TimescaleDB) and replays them
through the full agent pipeline using the paper broker.

Usage:
    uv run python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31
    uv run python scripts/run_backtest.py --start 2023-01-01 --sample-rate 0.2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backtest")


async def _load_candles(
    symbols:  list[str],
    start:    str,
    end:      str,
    interval: str = "5",
) -> dict[str, pd.DataFrame]:
    """Load OHLCV candles from TimescaleDB for all symbols in the date range."""
    from sqlalchemy import text, select
    from yukti.data.database import get_db
    from yukti.data.models import Candle

    candles: dict[str, pd.DataFrame] = {}

    async with get_db() as db:
        for symbol in symbols:
            result = await db.execute(
                select(Candle)
                .where(
                    Candle.symbol   == symbol,
                    Candle.interval == interval,
                    Candle.time     >= datetime.fromisoformat(start),
                    Candle.time     <= datetime.fromisoformat(end + " 23:59"),
                )
                .order_by(Candle.time)
            )
            rows = result.scalars().all()

            if not rows:
                log.warning("No candles for %s in [%s, %s]", symbol, start, end)
                continue

            df = pd.DataFrame(
                [(r.time, r.open, r.high, r.low, r.close, r.volume) for r in rows],
                columns=["time", "open", "high", "low", "close", "volume"],
            )
            df.set_index("time", inplace=True)
            df = df.astype(float)
            candles[symbol] = df
            log.info("Loaded %d candles for %s", len(df), symbol)

    return candles


async def main() -> None:
    parser = argparse.ArgumentParser(description="Yukti backtest runner")
    parser.add_argument("--start",       required=True,  help="Start date YYYY-MM-DD")
    parser.add_argument("--end",         default=str(date.today()), help="End date YYYY-MM-DD")
    parser.add_argument("--universe",    default="universe.json", help="Symbol→security_id JSON")
    parser.add_argument("--sample-rate", type=float, default=1.0,
                        help="Fraction of candles to send to Claude (0.1=10% to save API cost)")
    parser.add_argument("--out",         default="backtest_results", help="Output directory")
    args = parser.parse_args()

    # Load universe
    universe_path = Path(args.universe)
    if not universe_path.exists():
        log.error("universe.json not found — run scripts/universe_loader.py --sample first")
        return

    universe: dict[str, str] = json.loads(universe_path.read_text())
    symbols = list(universe.keys())
    log.info("Backtest: %d symbols, %s → %s, sample_rate=%.1f",
             len(symbols), args.start, args.end, args.sample_rate)

    # Load candles
    stock_candles = await _load_candles(symbols, args.start, args.end)
    nifty_candles = (await _load_candles(["NIFTY"], args.start, args.end)).get("NIFTY")

    if not stock_candles:
        log.error("No candle data found. Populate the candles table first.")
        return

    if nifty_candles is None:
        log.warning("No Nifty candles found — using first available symbol as proxy")
        nifty_candles = next(iter(stock_candles.values()))

    # Run backtest
    from yukti.backtest import BacktestEngine

    engine = BacktestEngine(
        candles            = stock_candles,
        nifty_candles      = nifty_candles,
        account_value      = 500_000.0,
        claude_sample_rate = args.sample_rate,
    )

    log.info("Running backtest...")
    report = await engine.run()

    # Print summary
    report.print_summary()

    # Save results
    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True)

    trades_path = out_dir / f"trades_{args.start}_{args.end}.csv"
    equity_path = out_dir / f"equity_{args.start}_{args.end}.csv"

    report.to_csv(str(trades_path))
    report.equity_curve.to_csv(str(equity_path))

    log.info("Results saved to %s/", out_dir)


if __name__ == "__main__":
    asyncio.run(main())
