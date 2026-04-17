"""
yukti/scheduler/jobs.py
APScheduler cron jobs and NSE trading calendar.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger(__name__)

# ── NSE holidays (update annually from NSE circular) ─────────────────────────
NSE_HOLIDAYS: set[date] = {
    date(2025, 1, 26), date(2025, 3, 14), date(2025, 4, 14),
    date(2025, 4, 18), date(2025, 5, 1),  date(2025, 8, 15),
    date(2025, 10, 2), date(2025, 10, 24), date(2025, 11, 5),
    date(2025, 12, 25),
    date(2026, 1, 26), date(2026, 8, 15), date(2026, 10, 2),
    date(2026, 12, 25),
}


def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def is_trading_hours(now: datetime | None = None) -> bool:
    t = (now or datetime.now()).time()
    return time(9, 15) <= t <= time(15, 10)


def is_fo_expiry(d: date | None = None) -> bool:
    d = d or date.today()
    if d.weekday() != 3:
        return False
    return (d + timedelta(days=7)).month != d.month


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def job_morning_prep() -> None:
    log.info("=== morning prep ===")
    from yukti.execution.reconcile import reconcile_positions
    await reconcile_positions()


async def job_eod_squareoff() -> None:
    log.info("=== EOD squareoff ===")
    from yukti.data.state import get_all_positions
    from yukti.execution.dhan_client import dhan
    from yukti.execution.order_sm import close_trade

    for symbol, pos in (await get_all_positions()).items():
        if pos.get("holding_period") != "intraday":
            continue
        if pos.get("status") not in ("ARMED", "FILLED"):
            continue
        sec  = pos.get("security_id", "")
        qty  = int(pos.get("quantity", 0))
        dirn = pos.get("direction", "LONG")
        try:
            for gtt in [pos.get("sl_gtt_id"), pos.get("target_gtt_id")]:
                if gtt:
                    await dhan.cancel_gtt(gtt)
            await dhan.market_exit(sec, dirn, qty, "INTRADAY")
            await close_trade(symbol, float(pos.get("entry_price", 0)), "eod_squareoff")
            log.info("EOD closed %s", symbol)
        except Exception as exc:
            log.error("EOD squareoff failed %s: %s", symbol, exc)


async def job_daily_reset() -> None:
    log.info("=== daily reset ===")
    from yukti.data.state import reset_daily_pnl, reset_trades_today
    await reset_daily_pnl()
    await reset_trades_today()
    log.info("Daily counters reset")
    log.info("=== daily journal ===")
    from datetime import date as dt_date
    from yukti.data.database import get_db
    from yukti.data.models import Trade
    from yukti.agents.journal import write_journal_entry
    from yukti.agents.memory import store_journal
    from sqlalchemy import select, func as sa_func

    today = dt_date.today()
    async with get_db() as db:
        rows = (await db.execute(
            select(Trade).where(
                sa_func.date(Trade.closed_at) == today,
                Trade.pnl.is_not(None),
            )
        )).scalars().all()

    for t in rows:
        try:
            text = await write_journal_entry(
                symbol=t.symbol, direction=t.direction, setup_type=t.setup_type,
                entry=t.entry_price, stop_loss=t.stop_loss, target=t.target_1,
                exit_price=t.exit_price or t.entry_price,
                exit_reason=t.exit_reason or "", pnl_pct=t.pnl_pct or 0.0,
                conviction=t.conviction, reasoning=t.reasoning,
            )
            await store_journal(t.id, t.symbol, t.setup_type, t.direction,
                                t.pnl_pct or 0.0, text)
        except Exception as exc:
            log.error("Journal failed trade %d: %s", t.id, exc)


async def job_daily_report() -> None:
    from yukti.data.state import get_performance_state
    from yukti.telegram.bot import alert
    perf = await get_performance_state()
    icon = "✅" if perf["daily_pnl_pct"] >= 0 else "❌"
    await alert(
        f"{icon} *Yukti Daily Summary*\n"
        f"P&L: {perf['daily_pnl_pct']:+.2f}% | Trades: {perf['trades_today']}\n"
        f"Win rate (L10): {perf['win_rate_last_10']:.0%} | "
        f"Streak losses: {perf['consecutive_losses']}"
    )


def build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="Asia/Kolkata")
    sched.add_job(job_morning_prep,  "cron", hour=9,  minute=0)
    sched.add_job(job_eod_squareoff, "cron", hour=15, minute=10)
    sched.add_job(job_daily_journal, "cron", hour=16, minute=0)
    sched.add_job(job_daily_report,  "cron", hour=16, minute=30)
    return sched
