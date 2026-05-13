import asyncio
import os

os.environ["MODE"] = "live"

from yukti.services.control_plane_service import ControlPlaneService
from yukti.execution.monitor import monitor_positions, _on_tick, _feed_subscribed
from yukti.execution.live_feed import get_feed_manager
from yukti.data.models import Position, PositionStatus, Direction, HoldingPeriod

async def run_probe():
    service = ControlPlaneService()
    feed = get_feed_manager()
    
    symbol = "RELIANCE_PROBE"
    security_id = "1333"
    
    # Save a synthetic ARMED position
    pos = Position(
        symbol=symbol,
        security_id=security_id,
        direction=Direction.LONG,
        entry_price=700.0,
        fill_price=700.0,
        stop_loss=1.0,
        target_1=999999.0,
        quantity=1,
        holding_period=HoldingPeriod.INTRADAY,
        conviction=7,
        risk_reward=2.0,
        status=PositionStatus.ARMED,
        strategy_id="PROBE",
        signal_id="PROBE_SIG",
        reasoning="Integration probe"
    )
    
    print(f"Saving synthetic position for {symbol}...")
    await service.save_position(pos)
    
    # Start the feed
    print("Starting feed connection...")
    asyncio.create_task(feed.connect(_on_tick))
    await asyncio.sleep(1) # wait for connection
    
    # Start monitor_positions as a task
    print("Starting monitor_positions task...")
    monitor_task = asyncio.create_task(monitor_positions(poll_interval=2))
    
    # Wait for about 6 seconds
    print("Waiting 6 seconds for subscription...")
    await asyncio.sleep(6)
    
    # Check subscriptions
    # Note: _feed_subscribed stores security_ids
    subscribed_in_set = security_id in _feed_subscribed
    subscribed_in_feed = security_id in feed._subscriptions
    
    print(f"Result: {symbol} (ID: {security_id})")
    print(f"Present in _feed_subscribed: {subscribed_in_set}")
    print(f"Present in feed._subscriptions: {subscribed_in_feed}")
    
    # Cleanup
    print("Cleaning up...")
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
        
    await service.delete_position(symbol)
    await feed.disconnect()
    print("Cleanup complete.")

if __name__ == "__main__":
    asyncio.run(run_probe())
