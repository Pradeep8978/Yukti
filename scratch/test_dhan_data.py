import asyncio
import logging
from datetime import datetime, timedelta
from yukti.execution.dhan_client import DhanClient
from yukti.config import settings

async def test_data():
    logging.basicConfig(level=logging.INFO)
    dhan = DhanClient()
    
    # Try fetching RELIANCE (2885) for a known trading day (last Friday was April 24)
    # Today is Sunday April 26.
    start = "2026-04-20"
    end = "2026-04-24"
    
    print(f"Fetching RELIANCE (2885) from {start} to {end}...")
    try:
        raw = await dhan.get_candles("2885", "5", start, end, symbol="RELIANCE")
        print(f"Result: {len(raw)} candles")
        if raw:
            print(f"Sample: {raw[0]}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_data())
