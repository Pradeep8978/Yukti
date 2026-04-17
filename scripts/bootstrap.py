"""
scripts/bootstrap.py
First-time database setup for Yukti.
Run once after docker compose up -d redis postgres.

Usage:
    uv run python scripts/bootstrap.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path


async def _create_extensions() -> None:
    """Enable pgvector and TimescaleDB in the Yukti database."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from yukti.config import settings

    engine = create_async_engine(settings.postgres_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        print("  ✅ Extensions enabled: vector, timescaledb")
    await engine.dispose()


async def _run_migrations() -> None:
    """Run Alembic migrations to create all tables."""
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ❌ Migration failed:\n{result.stderr}")
        sys.exit(1)
    print("  ✅ Migrations applied")


async def _create_hypertable() -> None:
    """Convert candles table to TimescaleDB hypertable."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from yukti.config import settings

    engine = create_async_engine(settings.postgres_url)
    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "SELECT create_hypertable('candles', 'time', if_not_exists => TRUE)"
            ))
            print("  ✅ Candles TimescaleDB hypertable created")
        except Exception as exc:
            print(f"  ⚠️  Hypertable (may already exist): {exc}")
    await engine.dispose()


async def _verify_redis() -> None:
    import redis.asyncio as aioredis
    from yukti.config import settings

    r = await aioredis.from_url(settings.redis_url, decode_responses=True)
    await r.ping()
    await r.aclose()
    print("  ✅ Redis connection OK")


async def main() -> None:
    print("\n🚀 Yukti Bootstrap\n")

    print("1/4  Verifying Redis...")
    await _verify_redis()

    print("2/4  Enabling PostgreSQL extensions...")
    await _create_extensions()

    print("3/4  Running Alembic migrations...")
    await _run_migrations()

    print("4/4  Creating TimescaleDB hypertable...")
    await _create_hypertable()

    print("\n✅ Bootstrap complete. Run:\n")
    print("    uv run python scripts/universe_loader.py --file universe.csv")
    print("    uv run python -m yukti --mode paper\n")


if __name__ == "__main__":
    asyncio.run(main())
