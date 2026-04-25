import asyncio
from dhanhq import dhanhq, DhanContext
from yukti.config import settings

async def test_raw_prod():
    ctx = DhanContext(client_id=settings.dhan_client_id, access_token=settings.dhan_access_token)
    # Ensure production URL
    ctx.dhan_http.base_url = "https://api.dhan.co/v2"
    d = dhanhq(ctx)
    
    start = "2026-04-20"
    end = "2026-04-24"
    
    print(f"Calling intraday_minute_data on PRODUCTION for RELIANCE (2885)...")
    res = d.intraday_minute_data(
        security_id="2885",
        exchange_segment="NSE_EQ",
        instrument_type="EQUITY",
        from_date=start,
        to_date=end,
        interval=5
    )
    print(f"Type: {type(res)}")
    print(f"Raw Response: {res}")

if __name__ == "__main__":
    asyncio.run(test_raw_prod())
