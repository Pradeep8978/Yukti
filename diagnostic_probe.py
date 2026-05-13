import asyncio
import os
from datetime import datetime

os.environ["MODE"] = "live"

from yukti.data.state import save_position, delete_position, get_all_positions
from yukti.execution.live_feed import get_feed_manager, _feed_subscribed
from yukti.execution.monitor import monitor_positions

async def main():
    symbol = "RELIANCE_PROBE"
    security_id = "2885"
    
    pos_data = {
        "symbol": symbol,
        "security_id": security_id,
        "exchange": "NSE_EQ",
        "direction": "LONG",
        "setup_type": "BREAKOUT",
        "holding_period": "INTRADAY",
        "status": "ARMED",
        "entry_price": 2500.0,
        "stop_loss": 2450.0,
        "target_1": 2600.0,
        "quantity": 10,
        "intent_id": None,
        "opened_at": datetime.utcnow().isoformat()
    }

    print("--- Saving position ---")
    await save_position(symbol, pos_data)
    
    all_positions = await get_all_positions()
    print(f"Is {symbol} in get_all_positions()? {symbol in all_positions}")

    feed = get_feed_manager()
    
    print("--- Starting feed ---")
    # feed.connect is often not an async task but a method that sets up things
    # Let's try to just connect
    try:
        if asyncio.iscoroutinefunction(feed.connect):
            asyncio.create_task(feed.connect())
        else:
            feed.connect()
    except Exception as e:
        print(f"Error starting feed: {e}")

    await asyncio.sleep(2)
    print(f"Feed connected? {feed.is_connected()}")

    print("--- Starting monitor task ---")
    monitor_task = asyncio.create_task(monitor_positions(poll_interval=2))
    
    print("--- Sleeping 5s ---")
    await asyncio.sleep(5)
    
    print(f"Monitor task done? {monitor_task.done()}")
    if monitor_task.done():
        try:
            exc = monitor_task.exception()
            print(f"Task exception: {exc}")
        except Exception as e:
            print(f"Error getting exception: {e}")

    print(f"_feed_subscribed content: {_feed_subscribed}")
    try:
        # Check if feed has subscriptions attribute
        subs = getattr(feed, "_subscriptions", "Attribute not found")
        print(f"feed._subscriptions: {subs}")
    except Exception as e:
        print(f"Error accessing feed._subscriptions: {e}")

    print("--- Cleanup ---")
    monitor_task.cancel()
    await delete_position(symbol)
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
