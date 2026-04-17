"""
yukti/execution/monitor.py   — live position watcher
yukti/agents/journal.py      — post-trade reflection writer
yukti/agents/memory.py       — pgvector semantic retrieval
yukti/scheduler/jobs.py      — APScheduler cron jobs
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, date
from typing import Any

import anthropic
import voyageai

from yukti.config import settings
from yukti.data.state import get_all_positions
from yukti.execution.order_sm import close_trade
from yukti.execution.dhan_client import dhan

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  POSITION MONITOR
# ═══════════════════════════════════════════════════════════════

async def monitor_positions(poll_interval: int = 10) -> None:
    """
    Background loop that polls all open positions every N seconds.
    Detects SL hits, target hits, and triggers close_trade accordingly.
    Should run as an asyncio task alongside the main signal loop.
    """
    log.info("Position monitor started (poll every %ds)", poll_interval)

    while True:
        await asyncio.sleep(poll_interval)

        positions = await get_all_positions()
        if not positions:
            continue

        for symbol, pos in positions.items():
            if pos.get("status") not in ("ARMED", "FILLED"):
                continue

            security_id = pos.get("security_id", "")
            try:
                candles = await dhan.get_candles(security_id, interval="1")
                if not candles:
                    continue
                last_close = float(candles[-1].get("close", 0))
            except Exception as exc:
                log.warning("Monitor: failed to fetch price for %s: %s", symbol, exc)
                continue

            is_long    = pos.get("direction") == "LONG"
            stop_loss  = float(pos.get("stop_loss", 0))
            target_1   = float(pos.get("target_1", 0))

            if is_long:
                if last_close <= stop_loss:
                    log.info("SL hit for %s @ ₹%.2f", symbol, last_close)
                    await close_trade(symbol, last_close, "stop_loss_hit")
                elif target_1 and last_close >= target_1:
                    log.info("Target 1 hit for %s @ ₹%.2f", symbol, last_close)
                    await close_trade(symbol, last_close, "target_1_hit")
            else:
                if last_close >= stop_loss:
                    log.info("SL hit (short) for %s @ ₹%.2f", symbol, last_close)
                    await close_trade(symbol, last_close, "stop_loss_hit")
                elif target_1 and last_close <= target_1:
                    log.info("Target 1 hit (short) for %s @ ₹%.2f", symbol, last_close)
                    await close_trade(symbol, last_close, "target_1_hit")


# ═══════════════════════════════════════════════════════════════
#  TRADE JOURNAL
# ═══════════════════════════════════════════════════════════════

async def write_journal_entry(
    trade:              dict[str, Any],
    original_reasoning: str,
) -> str:
    """
    Claude writes a 4-sentence post-trade reflection.
    Returns the journal text string.
    """
    entry     = float(trade.get("fill_price") or trade.get("entry_price", 0))
    exit_p    = float(trade.get("exit_price", 0))
    pnl_pct   = float(trade.get("pnl_pct", 0))
    symbol    = trade.get("symbol", "")
    direction = trade.get("direction", "")
    setup     = trade.get("setup_type", "")
    sl        = float(trade.get("stop_loss", 0))
    t1        = float(trade.get("target_1", 0))
    conv      = int(trade.get("conviction", 0))
    reason    = trade.get("exit_reason", "")

    prompt = f"""A trade just closed. Write a 4-sentence reflective journal entry.

Trade:
  Symbol      : {symbol}
  Direction   : {direction}
  Setup       : {setup}
  Entry       : ₹{entry:.2f} | SL ₹{sl:.2f} | Target ₹{t1:.2f}
  Exit        : ₹{exit_p:.2f} ({reason})
  P&L         : {pnl_pct:+.2f}%
  Conviction  : {conv}/10
  Reasoning at entry: "{original_reasoning}"

Write exactly 4 sentences:
1. What the setup was and why I entered.
2. What happened during the trade.
3. Why it worked / why it failed — be specific and honest.
4. One concrete thing I will do differently next time.

Be direct. No filler. First person."""

    client   = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model      = settings.claude_model,
        max_tokens = 300,
        messages   = [{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    log.info("Journal written for %s", symbol)
    return text


# ═══════════════════════════════════════════════════════════════
#  VECTOR MEMORY
# ═══════════════════════════════════════════════════════════════

_voyage: voyageai.Client | None = None

def _get_voyage() -> voyageai.Client:
    global _voyage
    if _voyage is None:
        _voyage = voyageai.Client(api_key=settings.voyage_api_key)
    return _voyage


async def embed_journal(text: str) -> list[float]:
    """Generate Voyage AI embedding for a journal entry."""
    vc = _get_voyage()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: vc.embed([text], model="voyage-large-2-instruct", input_type="document"),
    )
    return result.embeddings[0]


async def retrieve_similar(
    symbol:     str,
    setup_type: str,
    direction:  str,
    top_k:      int = 3,
) -> str:
    """
    Find the top-k most similar past journal entries via pgvector cosine similarity.
    Returns a formatted string for injection into Claude's context.
    """
    from sqlalchemy import text as sa_text
    from yukti.data.database import get_db

    query_text = f"{symbol} {direction} {setup_type} trade on NSE"
    loop       = asyncio.get_event_loop()
    vc         = _get_voyage()

    query_emb = await loop.run_in_executor(
        None,
        lambda: vc.embed([query_text], model="voyage-large-2-instruct", input_type="query").embeddings[0],
    )
    emb_str = str(query_emb)

    sql = sa_text("""
        SELECT entry_text, pnl_pct, setup_type, direction, symbol
        FROM journal_entries
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> :emb ::vector
        LIMIT :k
    """)

    results = []
    async with get_db() as db:
        rows = (await db.execute(sql, {"emb": emb_str, "k": top_k})).fetchall()
        for row in rows:
            pnl_label = "WIN" if row.pnl_pct > 0 else "LOSS"
            results.append(
                f"[{pnl_label} {row.pnl_pct:+.1f}%] {row.symbol} {row.direction} {row.setup_type}: {row.entry_text}"
            )

    if not results:
        return ""

    return "Past similar setups:\n" + "\n\n".join(f"  {i+1}. {r}" for i, r in enumerate(results))


# ═══════════════════════════════════════════════════════════════
#  NSE TRADING CALENDAR
# ═══════════════════════════════════════════════════════════════

# NSE holidays 2025 (update annually)
NSE_HOLIDAYS_2025 = {
    date(2025, 1, 26),   # Republic Day
    date(2025, 3, 14),   # Holi
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 10, 2),   # Gandhi Jayanti
    date(2025, 10, 24),  # Diwali (Dussehra)
    date(2025, 11, 5),   # Diwali Laxmi Pujan
    date(2025, 12, 25),  # Christmas
}

NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 12, 25),  # Christmas
    # Add others from NSE circular
}

ALL_HOLIDAYS = NSE_HOLIDAYS_2025 | NSE_HOLIDAYS_2026


def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    if d.weekday() >= 5:      # Saturday=5, Sunday=6
        return False
    return d not in ALL_HOLIDAYS


def is_trading_hours() -> bool:
    """True if current time is within normal NSE trading window."""
    from datetime import time
    now = datetime.now().time()
    return time(9, 15) <= now <= time(15, 10)


# ═══════════════════════════════════════════════════════════════
#  SCHEDULER JOBS
# ═══════════════════════════════════════════════════════════════

async def job_morning_prep() -> None:
    """09:00 — reconcile positions, reset daily state."""
    from yukti.execution.reconcile import reconcile_positions
    log.info("=== Morning prep ===")
    await reconcile_positions()


async def job_eod_squareoff() -> None:
    """15:10 — force close all intraday positions at market."""
    log.info("=== EOD squareoff ===")
    positions = await get_all_positions()
    for symbol, pos in positions.items():
        if pos.get("holding_period") == "intraday" and pos.get("status") in ("ARMED", "FILLED"):
            security_id  = pos.get("security_id", "")
            direction    = pos.get("direction", "LONG")
            qty          = int(pos.get("quantity", 0))
            try:
                result = await dhan.market_exit(security_id, direction, qty, "INTRADAY")
                exit_p = float(pos.get("entry_price", 0))  # will be updated by monitor
                await close_trade(symbol, exit_p, "eod_squareoff")
                log.info("EOD squareoff: %s %d shares", symbol, qty)
            except Exception as exc:
                log.error("EOD squareoff failed for %s: %s", symbol, exc)


async def job_daily_journal(closed_trades: list[dict[str, Any]]) -> None:
    """16:00 — write journal entries and embed them for memory."""
    from yukti.data.database import get_db
    from yukti.data.models import JournalEntry
    log.info("=== Daily journal: %d trades ===", len(closed_trades))

    for trade in closed_trades:
        if trade.get("pnl") is None:
            continue
        journal_text = await write_journal_entry(
            trade,
            original_reasoning=trade.get("reasoning", ""),
        )
        embedding = await embed_journal(journal_text)

        async with get_db() as db:
            db.add(JournalEntry(
                trade_id   = trade.get("db_id", 0),
                symbol     = trade.get("symbol", ""),
                setup_type = trade.get("setup_type", ""),
                direction  = trade.get("direction", ""),
                pnl_pct    = float(trade.get("pnl_pct", 0)),
                entry_text = journal_text,
                embedding  = embedding,
            ))
        log.info("Journal saved for %s", trade.get("symbol"))
