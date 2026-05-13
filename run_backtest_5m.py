import redis
import json
import asyncio
import traceback
import os
from yukti.backtest import _run_backtest

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

        # Signature: _run_backtest(start, end, sample_rate, symbols, use_rules_engine, interval='1')
        start_date = "2026-04-13"
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
