import asyncio
from sqlalchemy import select, func
from yukti.data.database import get_db
from yukti.data.models import Trade, JournalEntry, Candle, Position

async def check_counts():
    async with get_db() as db:
        trades = (await db.execute(select(func.count(Trade.id)))).scalar()
        journal = (await db.execute(select(func.count(JournalEntry.id)))).scalar()
        candles = (await db.execute(select(func.count(Candle.id)))).scalar()
        positions = (await db.execute(select(func.count(Position.symbol)))).scalar()
        
        print(f"Database Stats:")
        print(f"  - Candles: {candles}")
        print(f"  - Trades: {trades}")
        print(f"  - Journal: {journal}")
        print(f"  - Positions: {positions}")

if __name__ == "__main__":
    asyncio.run(check_counts())
