import asyncio
from dhanhq import dhanhq, DhanContext
from yukti.config import settings

async def probe_sandbox():
    ctx = DhanContext(client_id=settings.dhan_client_id, access_token=settings.dhan_access_token)
    ctx.dhan_http.base_url = "https://sandbox.dhan.co/v2"
    d = dhanhq(ctx)
    
    # Try common symbols
    symbols = ["2885", "11536", "1594", "1333", "13", "3045"] # RELIANCE, TCS, INFY, HDFCBANK, NIFTY, SBIN
    
    # Try different time ranges
    # 1. Last 2 days
    # 2. Last month
    # 3. Exactly today
    
    ranges = [
        ("2026-04-20", "2026-04-24"),
        ("2026-04-01", "2026-04-10"),
    ]
    
    for start, end in ranges:
        for sid in symbols:
            print(f"Probing {sid} from {start} to {end}...")
            res = d.intraday_minute_data(
                security_id=sid,
                exchange_segment="NSE_EQ" if sid != "13" else "IDX_I",
                instrument_type="EQUITY" if sid != "13" else "INDEX",
                from_date=start,
                to_date=end,
                interval=5
            )
            if res.get('status') == 'success':
                data = res.get('data', [])
                print(f"  SUCCESS! Found {len(data)} candles for {sid}")
                return
            else:
                print(f"  Failed: {res.get('remarks')}")

if __name__ == "__main__":
    asyncio.run(probe_sandbox())
