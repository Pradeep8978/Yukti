import asyncio
from dhanhq import dhanhq, DhanContext
from yukti.config import settings

async def test_quote():
    ctx = DhanContext(client_id=settings.dhan_client_id, access_token=settings.dhan_access_token)
    ctx.dhan_http.base_url = "https://sandbox.dhan.co/v2"
    d = dhanhq(ctx)
    
    print("Fetching Quote for RELIANCE (2885)...")
    res = d.quote_data(securities={'NSE_EQ': [2885]})
    print(f"Quote Response: {res.get('status')} - {res.get('remarks')}")
    if res.get('status') == 'success':
        print(f"Data: {res.get('data')}")

if __name__ == "__main__":
    asyncio.run(test_quote())
