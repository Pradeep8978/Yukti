import asyncio
from yukti.data.database import engine
from sqlalchemy import text

async def list_tables():
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))
        tables = [r[0] for r in res.fetchall()]
        print(f"Tables: {tables}")

if __name__ == "__main__":
    asyncio.run(list_tables())
