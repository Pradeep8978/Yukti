import asyncio
from yukti.data.database import get_db
from yukti.data.models import DailyPerformance
from sqlalchemy import select

async def check():
    async with get_db() as db:
        res = await db.execute(select(DailyPerformance))
        rows = res.scalars().all()
        print(f"Found {len(rows)} daily performance records")
        for r in rows:
            print(f"Date: {r.date}, PnL: {r.gross_pnl}")

if __name__ == "__main__":
    asyncio.run(check())
