import asyncio
import os
from yukti.context import DhanContext
from yukti.feed import MarketFeed
from yukti.constants import ExchangeSegment

async def main():
    cid = os.getenv('DHAN_CLIENT_ID')
    tok = os.getenv('DHAN_ACCESS_TOKEN')
    if not cid or not tok: return print('Missing creds')
    try:
        ctx = DhanContext(client_id=cid, access_token=tok)
        feed = MarketFeed(ctx)
        res = []
        def cb(m): 
            res.append(m)
            if len(res) <= 3: print(f'Data: {str(m)[:80]}')
        feed.on_ticker = cb
        feed.subscribe(ExchangeSegment.NSE_EQ, '1333', MarketFeed.Ticker)
        task = asyncio.create_task(feed.connect())
        await asyncio.sleep(15)
        await feed.disconnect()
        print(f'Total: {len(res)}')
        print('Status: Success' if res else 'Status: No messages (Market closed?)')
    except Exception as e: print(f'Error: {e}')

if __name__ == '__main__': asyncio.run(main())