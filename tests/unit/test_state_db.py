"""Unit tests for DB-backed state helpers in `yukti.data.state`.

These tests create a temporary in-memory SQLite async engine and
only create the `positions` table to avoid requiring Postgres-only
types (pgvector/JSONB). Redis interactions are mocked with a
lightweight in-memory fake Redis implementation.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from yukti.data.models import Position
from yukti.data import state


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, ex: int | None = None):
        # store as string to mimic Redis client behavior
        self.store[key] = str(value)
        if ex is not None:
            self.ttls[key] = int(ex)

    async def delete(self, key: str):
        self.store.pop(key, None)
        self.ttls.pop(key, None)

    async def incr(self, key: str):
        val = int(self.store.get(key, "0")) + 1
        self.store[key] = str(val)
        return val

    async def incrbyfloat(self, key: str, amount: float):
        val = float(self.store.get(key, "0")) + float(amount)
        # keep string form in backing store
        self.store[key] = str(val)
        return val

    async def expire(self, key: str, ttl: int):
        self.ttls[key] = int(ttl)

    async def exists(self, key: str):
        return 1 if key in self.store else 0


@pytest.fixture
async def async_db():
    """Create an in-memory async SQLite engine and expose a get_db
    async contextmanager compatible with the project's `get_db`.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    AsyncSessionLocal = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, future=True
    )

    # create only the positions table to avoid PG-only types
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Position.__table__.create(sync_conn))

    @asynccontextmanager
    async def _get_db():
        async with AsyncSessionLocal() as session:
            yield session

    yield _get_db

    await engine.dispose()


@pytest.fixture
async def fake_redis():
    return FakeRedis()


@pytest.mark.asyncio
async def test_save_get_delete_position(async_db, fake_redis, monkeypatch):
    # wire test DB + fake Redis into state module
    monkeypatch.setattr(state, "get_db", async_db)

    async def _get_redis():
        return fake_redis

    monkeypatch.setattr(state, "get_redis", _get_redis)

    data = {
        "security_id": "SEC123",
        "direction": "LONG",
        "setup_type": "BREAKOUT",
        "holding_period": "intraday",
        "entry_price": 100.0,
        "fill_price": None,
        "stop_loss": 98.0,
        "target_1": 105.0,
        "target_2": None,
        "quantity": 10,
        "conviction": 7,
        "risk_reward": 2.5,
        "intent_id": None,
        "entry_order_id": None,
        "sl_gtt_id": None,
        "target_gtt_id": None,
        "status": "OPEN",
        "reasoning": "unit-test",
    }

    await state.save_position("ABC", data)
    pos = await state.get_position("ABC")
    assert pos is not None
    assert pos["symbol"] == "ABC"
    assert pos["quantity"] == 10

    all_pos = await state.get_all_positions()
    assert "ABC" in all_pos

    cnt = await state.count_open_positions()
    assert cnt == 1

    await state.delete_position("ABC")
    pos2 = await state.get_position("ABC")
    assert pos2 is None


@pytest.mark.asyncio
async def test_increment_trades_today(fake_redis, monkeypatch):
    async def _get_redis():
        return fake_redis

    monkeypatch.setattr(state, "get_redis", _get_redis)

    c1 = await state.increment_trades_today()
    assert int(c1) == 1
    c2 = await state.increment_trades_today()
    assert int(c2) == 2


@pytest.mark.asyncio
async def test_daily_pnl_and_performance(fake_redis, monkeypatch):
    async def _get_redis():
        return fake_redis

    monkeypatch.setattr(state, "get_redis", _get_redis)

    v = await state.add_to_daily_pnl(1.5)
    assert isinstance(v, float) and v == 1.5

    v2 = await state.add_to_daily_pnl(-0.5)
    assert v2 == pytest.approx(1.0)

    await state.record_trade_outcome(True)
    await state.record_trade_outcome(False)
    perf = await state.get_performance_state()
    assert "consecutive_losses" in perf and "daily_pnl_pct" in perf
