import asyncio
import os
from yukti.context import DhanContext
from yukti.feed import MarketFeed
from yukti.constants import ExchangeSegment

async def main():
    client_id = os.getenv("DHAN_CLIENT_ID")
    access_token = os.getenv("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        print("Missing credentials.")
        return
    try:
        ctx = DhanContext(client_id=client_id, access_token=access_token)
        feed = MarketFeed(ctx)
        msg_count = 0
        payloads = []
        def on_ticker(message):
            nonlocal msg_count
            msg_count += 1
            if len(payloads) < 3:
                payloads.append(str(message))
        feed.on_ticker = on_ticker
        print("Subscribing to RELIANCE (1333)...")
        feed.subscribe(ExchangeSegment.NSE_EQ, "1333", MarketFeed.Ticker)
        conn_task = asyncio.create_task(feed.connect())
        print("Waiting 15s...")
        await asyncio.sleep(15)
        print("Closing...")
        await feed.disconnect()
        conn_task.cancel()
        print(f"\nSummary: {msg_count} messages received.")
        for i, p in enumerate(payloads):
            print(f"P{i+1}: {p[:100]}...")
        if msg_count == 0:
            print("Status: No messages. Market may be closed.")
        else:
            print("Status: Success.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
