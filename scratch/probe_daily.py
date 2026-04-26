import asyncio
from dhanhq import dhanhq, DhanContext
from yukti.config import settings

async def probe_daily():
    ctx = DhanContext(client_id=settings.dhan_client_id, access_token=settings.dhan_access_token)
    ctx.dhan_http.base_url = "https://sandbox.dhan.co/v2"
    d = dhanhq(ctx)
    
    # Try Daily data
    print("Probing Daily data for RELIANCE...")
    res = d.historical_daily_data(
        security_id="2885",
        exchange_segment="NSE_EQ",
        instrument_type="EQUITY",
        from_date="2026-01-01",
        to_date="2026-04-20"
    )
    print(f"Daily Response: {res.get('status')} - {res.get('remarks')}")
    if res.get('status') == 'success':
        print(f"Data count: {len(res.get('data', []))}")

if __name__ == "__main__":
    asyncio.run(probe_daily())
