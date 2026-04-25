import asyncio
from dhanhq import dhanhq, DhanContext
from yukti.config import settings

async def test_raw_sandbox_v2():
    ctx = DhanContext(client_id=settings.dhan_client_id, access_token=settings.dhan_access_token)
    ctx.dhan_http.base_url = "https://sandbox.dhan.co/v2"
    d = dhanhq(ctx)
    
    # Try different symbols and dates
    test_cases = [
        ("2885", "NSE_EQ", "EQUITY"), # RELIANCE
        ("11536", "NSE_EQ", "EQUITY"), # TCS
        ("1594", "NSE_EQ", "EQUITY"),  # INFY
        ("13", "IDX_I", "INDEX"),      # NIFTY 50
    ]
    
    start = "2026-04-10"
    end = "2026-04-15"
    
    for sid, exch, itype in test_cases:
        print(f"Fetching {sid} ({itype}) from {start} to {end}...")
        res = d.intraday_minute_data(
            security_id=sid,
            exchange_segment=exch,
            instrument_type=itype,
            from_date=start,
            to_date=end,
            interval=5
        )
        print(f"Response: {res.get('status')} - {res.get('remarks')}")
        if res.get('status') == 'success':
            print(f"Data count: {len(res.get('data', []))}")
            break

if __name__ == "__main__":
    asyncio.run(test_raw_sandbox_v2())
