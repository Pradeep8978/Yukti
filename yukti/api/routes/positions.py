"""
yukti/api/routes/positions.py
yukti/api/routes/control.py
yukti/api/routes/journal.py
FastAPI route handlers consumed by the Flutter mobile dashboard.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc

from yukti.data.database import get_db
from yukti.data.models import Trade, JournalEntry, DailyPerformance
from yukti.data.state import (
    get_all_positions,
    get_daily_pnl_pct,
    get_performance_state,
    is_halted,
    set_halt,
)

# ── Positions router ──────────────────────────────────────────────────────────

positions_router = APIRouter(prefix="/positions", tags=["positions"])


@positions_router.get("/")
async def get_positions() -> dict[str, Any]:
    return {"positions": await get_all_positions()}


@positions_router.get("/count")
async def position_count() -> dict[str, int]:
    positions = await get_all_positions()
    return {"count": len(positions)}


# ── P&L router ────────────────────────────────────────────────────────────────

pnl_router = APIRouter(prefix="/pnl", tags=["pnl"])


@pnl_router.get("/today")
async def pnl_today() -> dict[str, float]:
    return {"pnl_pct": await get_daily_pnl_pct()}


@pnl_router.get("/performance")
async def performance() -> dict[str, Any]:
    return await get_performance_state()


@pnl_router.get("/history")
async def pnl_history(days: int = 30) -> dict[str, Any]:
    async with get_db() as db:
        rows = (await db.execute(
            select(DailyPerformance)
            .order_by(desc(DailyPerformance.date))
            .limit(days)
        )).scalars().all()
    return {
        "history": [
            {
                "date":        str(r.date),
                "trades":      r.trades_taken,
                "win_rate":    r.win_rate,
                "gross_pnl":   r.gross_pnl,
                "profit_factor": r.profit_factor,
            }
            for r in rows
        ]
    }


# ── Trades router ─────────────────────────────────────────────────────────────

trades_router = APIRouter(prefix="/trades", tags=["trades"])


@trades_router.get("/")
async def list_trades(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    async with get_db() as db:
        rows = (await db.execute(
            select(Trade).order_by(desc(Trade.opened_at)).limit(limit).offset(offset)
        )).scalars().all()
    return {
        "trades": [
            {
                "id":         t.id,
                "symbol":     t.symbol,
                "direction":  t.direction,
                "setup_type": t.setup_type,
                "entry":      t.entry_price,
                "exit":       t.exit_price,
                "pnl_pct":    t.pnl_pct,
                "conviction": t.conviction,
                "status":     t.status,
                "opened_at":  t.opened_at.isoformat() if t.opened_at else None,
                "closed_at":  t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in rows
        ]
    }


# ── Journal router ────────────────────────────────────────────────────────────

journal_router = APIRouter(prefix="/journal", tags=["journal"])


@journal_router.get("/")
async def list_journal(limit: int = 20) -> dict[str, Any]:
    async with get_db() as db:
        rows = (await db.execute(
            select(JournalEntry).order_by(desc(JournalEntry.created_at)).limit(limit)
        )).scalars().all()
    return {
        "entries": [
            {
                "id":         r.id,
                "symbol":     r.symbol,
                "direction":  r.direction,
                "setup_type": r.setup_type,
                "pnl_pct":    r.pnl_pct,
                "entry_text": r.entry_text,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


# ── Control router ────────────────────────────────────────────────────────────

control_router = APIRouter(prefix="/control", tags=["control"])


class HaltRequest(BaseModel):
    reason: str = "manual"


@control_router.get("/status")
async def agent_status() -> dict[str, Any]:
    halted = await is_halted()
    perf   = await get_performance_state()
    return {"halted": halted, "perf": perf}


@control_router.post("/halt")
async def halt(req: HaltRequest) -> dict[str, Any]:
    await set_halt(True)
    return {"halted": True, "reason": req.reason}


@control_router.post("/resume")
async def resume() -> dict[str, Any]:
    await set_halt(False)
    return {"halted": False}


@control_router.post("/squareoff")
async def squareoff_all() -> dict[str, Any]:
    """Close all open positions at market. Also halts the agent."""
    await set_halt(True)
    positions = await get_all_positions()
    results: list[dict] = []

    from yukti.execution.dhan_client import dhan
    from yukti.execution.order_sm import close_trade

    for symbol, pos in positions.items():
        sec   = pos.get("security_id", "")
        dirn  = pos.get("direction", "LONG")
        qty   = int(pos.get("quantity", 0))
        ptype = "INTRADAY" if pos.get("holding_period") == "intraday" else "DELIVERY"
        try:
            await dhan.market_exit(sec, dirn, qty, ptype)
            await close_trade(symbol, float(pos.get("entry_price", 0)), "api_squareoff")
            results.append({"symbol": symbol, "ok": True})
        except Exception as exc:
            results.append({"symbol": symbol, "ok": False, "error": str(exc)})

    return {"halted": True, "results": results}
