import redis
import json
import asyncio
import traceback
import os
from yukti.backtest import _run_backtest
from yukti.config import settings

async def main():
    try:
        r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
        pool_raw = r.get("yukti:candidate_pool")
        if not pool_raw:
            print("ERROR: yukti:candidate_pool not found in Redis.")
            return
            
        pool = json.loads(pool_raw)
        
        # Sort by turnover_cr then atr_pct descending
        # Each item expected to be a dict
        pool.sort(key=lambda x: (x.get("turnover_cr", 0), x.get("atr_pct", 0)), reverse=True)
        
        top50 = [item["symbol"] for item in pool[:50]]
        
        print(f"CANDIDATE_COUNT: {len(top50)}")
        print(f"TOP_SYMBOLS: {top50[:5]}")
        
        if not top50:
            print("ERROR: Empty candidate pool")
            return

        # Override settings for intraday backtest
        settings.account_value = 500_000        # ₹5L — enough to trade NSE stocks
        settings.max_open_positions = 5         # intraday: fewer concurrent positions
        settings.max_trades_per_day = 10        # allow more setups per day
        settings.cooldown_cycles = 1            # 1 bar cooldown on intraday
        settings.min_rr = 1.5                   # T1=1.5R intraday (rules engine sets t2_r=2.5)
        settings.min_conviction = 7             # intraday needs higher quality — default 5 is too loose
        settings.risk_pct = 0.005               # 0.5% risk/trade: 5 × 0.5% = 2.5% max heat

        # Signature: _run_backtest(start, end, sample_rate, symbols, use_rules_engine, interval='1')
        # Use the full Dhan 5m window (~60 days) so we get ~120-180 trades per
        # the 3/day cap instead of 60 — a meaningful sample for PF / win-rate.
        start_date = "2026-03-20"
        end_date = "2026-05-13"
        
        try:
            await _run_backtest(
                start=start_date,
                end=end_date,
                sample_rate=0.0,
                symbols=top50,
                use_rules_engine=True,
                interval='5'
            )
            print("BACKTEST_COMPLETED: TRUE")
            
            if os.path.exists("backtest_trades.csv"):
                with open("backtest_trades.csv", "r") as f:
                    lines = f.readlines()
                    print(f"TRADES_CSV_ROWS: {len(lines)}")
            else:
                print("TRADES_CSV_EXISTS: FALSE")
                
        except Exception as e:
            print(f"BACKTEST_FAILED: {e}")
            traceback.print_exc()

    except Exception as e:
        print(f"ERROR: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
