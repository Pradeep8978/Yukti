"""
yukti/data/state.py
Redis hot-state helpers.

Key schema:
    yukti:halt                       → "1" if kill switch active
    yukti:positions:{symbol}         → JSON TradePosition
    yukti:cooldown:{symbol}          → "1" with TTL = N cycles
    yukti:pnl:daily                  → float (today's realised P&L %)
    yukti:pnl:streak                 → int (consecutive losses; negative)
    yukti:pnl:wins_last_10           → CSV of last 10 outcomes "1,0,1,..."
    yukti:trades:today               → int (trades placed today)
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis

from yukti.config import settings

# ── Singleton async Redis client ──────────────────────────────────────────────
_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis


# ── Halt / kill switch ────────────────────────────────────────────────────────

async def is_halted() -> bool:
    r = await get_redis()
    return await r.get("yukti:halt") == "1"


async def set_halt(halted: bool) -> None:
    r = await get_redis()
    if halted:
        await r.set("yukti:halt", "1")
    else:
        await r.delete("yukti:halt")


# ── Open positions ────────────────────────────────────────────────────────────

async def save_position(symbol: str, data: dict[str, Any]) -> None:
    r = await get_redis()
    await r.set(f"yukti:positions:{symbol}", json.dumps(data))


async def get_position(symbol: str) -> dict[str, Any] | None:
    r = await get_redis()
    raw = await r.get(f"yukti:positions:{symbol}")
    return json.loads(raw) if raw else None


async def delete_position(symbol: str) -> None:
    r = await get_redis()
    await r.delete(f"yukti:positions:{symbol}")


async def get_all_positions() -> dict[str, dict[str, Any]]:
    r = await get_redis()
    keys = await r.keys("yukti:positions:*")
    if not keys:
        return {}
    values = await r.mget(*keys)
    return {
        k.split(":")[-1]: json.loads(v)
        for k, v in zip(keys, values)
        if v is not None
    }


async def count_open_positions() -> int:
    r = await get_redis()
    keys = await r.keys("yukti:positions:*")
    return len(keys)


# ── Cooldown registry ─────────────────────────────────────────────────────────

async def set_cooldown(symbol: str, candle_interval_seconds: int = 300) -> None:
    """Block a symbol for N cycles after a trade."""
    r = await get_redis()
    ttl = settings.cooldown_cycles * candle_interval_seconds
    await r.set(f"yukti:cooldown:{symbol}", "1", ex=ttl)


async def is_on_cooldown(symbol: str) -> bool:
    r = await get_redis()
    return await r.exists(f"yukti:cooldown:{symbol}") == 1


# ── Daily P&L ─────────────────────────────────────────────────────────────────

async def get_daily_pnl_pct() -> float:
    r = await get_redis()
    raw = await r.get("yukti:pnl:daily")
    return float(raw) if raw else 0.0


async def add_to_daily_pnl(pnl_pct: float) -> float:
    """Increment daily P&L and return new total."""
    r = await get_redis()
    # INCRBYFLOAT stored as string; key expires at midnight (86400s)
    new_val = await r.incrbyfloat("yukti:pnl:daily", pnl_pct)
    await r.expire("yukti:pnl:daily", 86_400)
    return float(new_val)


async def reset_daily_pnl() -> None:
    r = await get_redis()
    await r.delete("yukti:pnl:daily")


# ── Consecutive loss streak ───────────────────────────────────────────────────

async def record_trade_outcome(won: bool) -> None:
    """Record a trade result and update streak and win-rate list."""
    r = await get_redis()

    # Streak: positive = consecutive wins, negative = consecutive losses
    streak_raw = await r.get("yukti:pnl:streak")
    streak = int(streak_raw) if streak_raw else 0

    if won:
        new_streak = max(streak, 0) + 1
    else:
        new_streak = min(streak, 0) - 1
    await r.set("yukti:pnl:streak", str(new_streak), ex=86_400 * 7)

    # Rolling last-10 wins list
    wins_raw = await r.get("yukti:pnl:wins_last_10")
    wins = list(wins_raw.split(",")) if wins_raw else []
    wins.append("1" if won else "0")
    wins = wins[-10:]  # keep last 10
    await r.set("yukti:pnl:wins_last_10", ",".join(wins), ex=86_400 * 30)


async def get_performance_state() -> dict[str, Any]:
    r = await get_redis()
    streak_raw = await r.get("yukti:pnl:streak")
    wins_raw   = await r.get("yukti:pnl:wins_last_10")
    trades_raw = await r.get("yukti:trades:today")
    pnl_raw    = await r.get("yukti:pnl:daily")

    streak = int(streak_raw) if streak_raw else 0
    wins   = [int(x) for x in wins_raw.split(",") if x] if wins_raw else []

    return {
        "consecutive_losses": abs(streak) if streak < 0 else 0,
        "daily_pnl_pct":      float(pnl_raw)  if pnl_raw    else 0.0,
        "win_rate_last_10":   sum(wins) / len(wins) if wins  else 0.5,
        "trades_today":       int(trades_raw)   if trades_raw else 0,
    }


async def increment_trades_today() -> int:
    r = await get_redis()
    count = await r.incr("yukti:trades:today")
    await r.expire("yukti:trades:today", 86_400)
    return count
