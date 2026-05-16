import redis
import json
import asyncio
import traceback
from yukti.backtest import _run_backtest

async def main():
    try:
        r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
        pool_raw = r.get("yukti:candidate_pool")
        if not pool_raw:
            print("ERROR: yukti:candidate_pool not found in Redis.")
            return
            
        pool = json.loads(pool_raw)
        symbols = [item["symbol"] if isinstance(item, dict) else item for item in pool]
        
        print(f"CANDIDATE_COUNT: {len(symbols)}")
        
        if not symbols:
            print("ERROR: Empty candidate pool")
            return

        # Temporarily widen capacity for daily backtest (revert after run)
        from yukti.config import settings
        orig_max_pos = settings.max_open_positions
        orig_cooldown = settings.cooldown_cycles
        settings.max_open_positions = 5
        settings.cooldown_cycles = 1

        # Signature: _run_backtest(start, end, sample_rate, symbols, use_rules_engine)
        start_date = "2026-04-13"
        end_date = "2026-05-13"
        
        try:
            await _run_backtest(
                start=start_date,
                end=end_date,
                sample_rate=0.0,
                symbols=symbols,
                use_rules_engine=True,
                interval="D",
            )
            print("BACKTEST_COMPLETED: TRUE")
        except Exception as e:
            print(f"BACKTEST_FAILED: {e}")
            traceback.print_exc()
        finally:
            settings.max_open_positions = orig_max_pos
            settings.cooldown_cycles = orig_cooldown

    except Exception as e:
        print(f"ERROR: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
