"""
yukti/execution/monitor.py   — live position watcher
yukti/agents/journal.py      — post-trade reflection writer
yukti/agents/memory.py       — pgvector semantic retrieval
yukti/scheduler/jobs.py      — APScheduler cron jobs
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import anthropic
import voyageai

from yukti.config import settings
from yukti.data.state import get_all_positions, save_position, add_to_daily_pnl, get_redis
from yukti.execution.order_sm import close_trade
from yukti.execution.broker_factory import get_broker
from yukti.execution.live_feed import get_feed_manager

# Trail SL 1.5% behind the high-water mark after T1 partial exit
TRAIL_PCT = 0.015

log = logging.getLogger(__name__)

# Symbols currently subscribed to the live WebSocket feed
_feed_subscribed: set[str] = set()
# Guard against concurrent close from tick handler + REST loop
_closing_symbols: set[str] = set()


# ═══════════════════════════════════════════════════════════════
#  POSITION MONITOR
# ═══════════════════════════════════════════════════════════════

async def _on_tick(symbol: str, ltp: float) -> None:
    """
    WebSocket LTP tick handler — checks SL/target instantly.
    Called from the daemon feed thread via run_coroutine_threadsafe.
    Duplicate-safe: get_position returns None after close_trade removes the position.
    """
    if symbol in _closing_symbols:
        return
    try:
        r = await get_redis()
        raw = await r.get(f"yukti:positions:{symbol}")
        if not raw:
            return
        pos = json.loads(raw)
        if pos.get("status") not in ("ARMED", "FILLED"):
            return

        is_long    = pos.get("direction") == "LONG"
        stop_loss  = float(pos.get("stop_loss") or 0)
        fill_price = float(pos.get("fill_price") or pos.get("entry_price") or 0)
        target_1   = float(pos.get("target_1") or 0)
        active_sl  = float(pos.get("trailing_sl") or stop_loss)

        partial_done = (
            (stop_loss >= fill_price) if is_long
            else (fill_price > 0 and stop_loss <= fill_price)
        )

        closed = False
        if is_long:
            if ltp <= active_sl:
                log.info("WS tick: SL hit for %s @ ₹%.2f (ltp=%.2f)", symbol, active_sl, ltp)
                _closing_symbols.add(symbol)
                try:
                    await close_trade(symbol, ltp, "stop_loss_hit")
                finally:
                    _closing_symbols.discard(symbol)
                closed = True
            elif target_1 and ltp >= target_1:
                if not partial_done:
                    log.info("WS tick: T1 hit for %s @ ₹%.2f — partial exit", symbol, ltp)
                    await _partial_exit_t1(symbol, pos, ltp)
                else:
                    log.info("WS tick: T2 hit for %s @ ₹%.2f", symbol, ltp)
                    _closing_symbols.add(symbol)
                    try:
                        await close_trade(symbol, ltp, "target_2_hit")
                    finally:
                        _closing_symbols.discard(symbol)
                    closed = True
        else:
            if ltp >= active_sl:
                log.info("WS tick: SL hit (short) for %s @ ₹%.2f (ltp=%.2f)", symbol, active_sl, ltp)
                _closing_symbols.add(symbol)
                try:
                    await close_trade(symbol, ltp, "stop_loss_hit")
                finally:
                    _closing_symbols.discard(symbol)
                closed = True
            elif target_1 and ltp <= target_1:
                if not partial_done:
                    log.info("WS tick: T1 hit (short) for %s @ ₹%.2f — partial exit", symbol, ltp)
                    await _partial_exit_t1(symbol, pos, ltp)
                else:
                    log.info("WS tick: T2 hit (short) for %s @ ₹%.2f", symbol, ltp)
                    _closing_symbols.add(symbol)
                    try:
                        await close_trade(symbol, ltp, "target_2_hit")
                    finally:
                        _closing_symbols.discard(symbol)
                    closed = True

        if closed:
            _feed_subscribed.discard(symbol)
            await get_feed_manager().unsubscribe(symbol)

    except Exception as exc:
        log.debug("WS tick handler error for %s: %s", symbol, exc)


async def monitor_positions(poll_interval: int = 10) -> None:
    """
    Background loop that polls all open positions every N seconds.
    Detects SL hits, target hits, and triggers close_trade accordingly.
    When the WebSocket feed is connected, ticks handle exits in <1s;
    this loop acts as the heartbeat and fallback when the feed is down.
    Should run as an asyncio task alongside the main signal loop.
    """
    log.info("Position monitor started (poll every %ds)", poll_interval)

    while True:
        await asyncio.sleep(poll_interval)

        # Quick broker-side check to catch fills executed via GTTs
        # which may not be immediately reflected in Redis/Postgres.
        try:
            broker = get_broker()
            broker_positions_raw = await broker.get_positions()
            broker_symbols = {bp.get("tradingSymbol") for bp in broker_positions_raw if int(bp.get("netQty", 0)) != 0}
        except Exception as exc:
            broker_symbols = set()
            log.debug("Monitor: could not fetch broker positions: %s", exc)

        positions = await get_all_positions()
        if not positions:
            continue

        # Merge Redis-only trailing_sl into each Postgres-sourced position dict.
        # get_all_positions() reads Postgres (authoritative) which has no trailing_sl
        # column; the field lives only in the Redis cache written by _update_trailing_sl.
        try:
            r = await get_redis()
            for sym, pd_ in positions.items():
                raw = await r.get(f"yukti:positions:{sym}")
                if raw:
                    cached = json.loads(raw)
                    if cached.get("trailing_sl"):
                        pd_["trailing_sl"] = cached["trailing_sl"]
        except Exception as exc:
            log.debug("Monitor: trailing_sl Redis merge failed: %s", exc)

        # ── Manage live feed subscriptions ────────────────────────────────
        feed = get_feed_manager()
        if feed.is_connected():
            active_symbols = {
                sym for sym, pd_ in positions.items()
                if pd_.get("status") in ("ARMED", "FILLED")
            }
            for sym in active_symbols - _feed_subscribed:
                sec_id = positions[sym].get("security_id", "")
                if sec_id:
                    await feed.subscribe(sym, sec_id)
                    _feed_subscribed.add(sym)
            for sym in _feed_subscribed - active_symbols:
                await feed.unsubscribe(sym)
                _feed_subscribed.discard(sym)

        for symbol, pos in positions.items():
            if pos.get("status") not in ("ARMED", "FILLED"):
                continue

            security_id = pos.get("security_id", "")
            # If broker no longer reports the symbol but our DB thinks ARMED/FILLED,
            # schedule an immediate reconciliation to avoid stale Redis state.
            if pos.get("status") in ("ARMED", "FILLED") and broker_symbols and symbol not in broker_symbols:
                log.warning("Monitor: broker has no position for %s but state=%s — scheduling reconciliation", symbol, pos.get("status"))
                try:
                    from yukti.execution.reconcile import reconcile_positions
                    asyncio.create_task(reconcile_positions())
                except Exception as exc:
                    log.error("Monitor: failed to schedule reconciliation: %s", exc)
                # Skip per-symbol market checks once we scheduled reconcile
                continue

            try:
                candles = await get_broker().get_candles(security_id, interval="1")
                if not candles:
                    continue
                last = candles[-1]
                last_high  = float(last.get("high",  0))
                last_low   = float(last.get("low",   0))
                last_close = float(last.get("close", 0))
            except Exception as exc:
                log.warning("Monitor: failed to fetch price for %s: %s", symbol, exc)
                continue

            is_long    = pos.get("direction") == "LONG"
            stop_loss  = float(pos.get("stop_loss", 0))
            fill_price = float(pos.get("fill_price") or pos.get("entry_price", 0))
            target_1   = float(pos.get("target_1") or 0)
            active_sl  = float(pos.get("trailing_sl") or stop_loss)

            # Detect whether T1 partial exit already happened:
            # after partial exit we move stop_loss to breakeven (fill_price).
            partial_done = (
                (stop_loss >= fill_price) if is_long
                else (fill_price > 0 and stop_loss <= fill_price)
            )

            if symbol in _closing_symbols:
                continue  # tick handler already closing this symbol

            if is_long:
                if last_low <= active_sl:
                    log.info("SL hit for %s @ ₹%.2f (trailing_sl=%.2f)", symbol, active_sl, active_sl)
                    _closing_symbols.add(symbol)
                    try:
                        await close_trade(symbol, active_sl, "stop_loss_hit")
                    finally:
                        _closing_symbols.discard(symbol)
                elif target_1 and last_high >= target_1:
                    if not partial_done:
                        log.info("T1 hit for %s @ ₹%.2f — partial exit", symbol, last_high)
                        await _partial_exit_t1(symbol, pos, target_1)
                    else:
                        log.info("T2 hit for %s @ ₹%.2f", symbol, last_high)
                        _closing_symbols.add(symbol)
                        try:
                            await close_trade(symbol, target_1, "target_2_hit")
                        finally:
                            _closing_symbols.discard(symbol)
                elif partial_done:
                    await _update_trailing_sl(symbol, pos, last_high, last_low, is_long=True)
            else:
                if last_high >= active_sl:
                    log.info("SL hit (short) for %s @ ₹%.2f (trailing_sl=%.2f)", symbol, active_sl, active_sl)
                    _closing_symbols.add(symbol)
                    try:
                        await close_trade(symbol, active_sl, "stop_loss_hit")
                    finally:
                        _closing_symbols.discard(symbol)
                elif target_1 and last_low <= target_1:
                    if not partial_done:
                        log.info("T1 hit (short) for %s @ ₹%.2f — partial exit", symbol, last_low)
                        await _partial_exit_t1(symbol, pos, target_1)
                    else:
                        log.info("T2 hit (short) for %s @ ₹%.2f", symbol, last_low)
                        _closing_symbols.add(symbol)
                        try:
                            await close_trade(symbol, target_1, "target_2_hit")
                        finally:
                            _closing_symbols.discard(symbol)
                elif partial_done:
                    await _update_trailing_sl(symbol, pos, last_high, last_low, is_long=False)


async def _partial_exit_t1(symbol: str, pos: dict[str, Any], exit_price: float) -> None:
    """
    Sell half the position at T1, move SL to breakeven, trail the remainder.

    After this call the position record reflects the remaining half:
      - quantity   → remaining shares
      - stop_loss  → fill_price (breakeven) — doubles as the crash-safe
                     "partial exit done" marker read by monitor_positions
      - target_1   → old target_2 (0 if none)
      - target_2   → None
      - sl_gtt_id  → new breakeven GTT for remaining shares
      - trailing_sl→ T1 * (1 ± TRAIL_PCT) in Redis
    """
    qty       = int(pos.get("quantity", 0))
    half_qty  = qty // 2
    remaining = qty - half_qty
    if half_qty == 0:
        log.warning("Partial exit %s: quantity %d too small to split", symbol, qty)
        return

    direction    = pos.get("direction", "LONG")
    is_long      = direction == "LONG"
    security_id  = pos.get("security_id", "")
    product_type = "INTRADAY" if pos.get("holding_period") == "intraday" else "DELIVERY"
    fill_price   = float(pos.get("fill_price") or pos.get("entry_price", 0))
    exit_side    = "SELL" if is_long else "BUY"

    # 1 — Cancel T1 GTT BEFORE placing the partial market exit so the broker
    #     cannot fill it for the full quantity concurrently with our half-exit.
    if pos.get("target_gtt_id"):
        try:
            await get_broker().cancel_gtt(pos["target_gtt_id"])
        except Exception as exc:
            log.warning("Partial exit: cancel T1 GTT failed for %s: %s", symbol, exc)

    # 2 — Place partial market exit
    try:
        await get_broker().market_exit(security_id, direction, half_qty, product_type)
    except Exception as exc:
        log.error("Partial exit order failed for %s: %s", symbol, exc)
        return

    # 3 — Record partial P&L
    partial_pnl = (
        (exit_price - fill_price) * half_qty if is_long
        else (fill_price - exit_price) * half_qty
    )
    orig_exposure = fill_price * qty
    await add_to_daily_pnl(partial_pnl / orig_exposure * 100 if orig_exposure else 0.0)

    # 4 — Replace SL GTT: cancel full-qty one, arm breakeven GTT for remaining.
    #     If the new GTT cannot be armed, the remaining position has no broker-side
    #     crash-stop — market-exit it immediately rather than leave it naked.
    if pos.get("sl_gtt_id"):
        try:
            await get_broker().cancel_gtt(pos["sl_gtt_id"])
        except Exception as exc:
            log.warning("Partial exit: cancel old SL GTT failed for %s: %s", symbol, exc)

    new_sl_gtt_id: str | None = None
    try:
        gtt_resp = await get_broker().place_gtt(
            security_id      = security_id,
            transaction_type = exit_side,
            quantity         = remaining,
            trigger_price    = fill_price,
            order_type       = "SL-M",
            product_type     = product_type,
        )
        if isinstance(gtt_resp, dict):
            new_sl_gtt_id = (
                gtt_resp.get("gttOrderId")
                or (gtt_resp.get("data") or {}).get("gttOrderId")
            )
    except Exception as exc:
        log.warning("Partial exit: breakeven SL GTT failed for %s: %s", symbol, exc)

    if not new_sl_gtt_id:
        log.error("Partial exit: no breakeven SL GTT for %s — safety-exiting remaining %d shares", symbol, remaining)
        try:
            await get_broker().market_exit(security_id, direction, remaining, product_type)
            await close_trade(symbol, exit_price, "partial_no_sl_gtt_safety_exit")
        except Exception as safety_exc:
            log.critical("Partial exit safety exit also failed for %s: %s", symbol, safety_exc)
            try:
                from yukti.telegram.bot import alert
                await alert(
                    f"🚨 *CRITICAL — {symbol}*\n"
                    f"{remaining} shares have no SL protection and safety exit failed.\n"
                    f"*MANUAL INTERVENTION REQUIRED.*"
                )
            except Exception:
                pass
        return

    # 5 — Update position record; promote T2 → T1 for next monitor check
    old_t2 = float(pos.get("target_2") or 0)
    pos["quantity"]      = remaining
    pos["stop_loss"]     = fill_price          # breakeven — crash-safe "partial done" marker
    pos["target_1"]      = old_t2             # 0 if no T2 → monitor's "target_1 and ..." skips it
    pos["target_2"]      = None
    pos["sl_gtt_id"]     = new_sl_gtt_id
    pos["target_gtt_id"] = None
    pos["trailing_sl"]   = round(
        exit_price * (1 - TRAIL_PCT) if is_long else exit_price * (1 + TRAIL_PCT), 2
    )
    await save_position(symbol, pos)

    pnl_pct = partial_pnl / orig_exposure * 100 if orig_exposure else 0.0
    log.info(
        "Partial exit %s: sold %d @ ₹%.2f (P&L %.2f%%), holding %d | new SL ₹%.2f (breakeven)",
        symbol, half_qty, exit_price, pnl_pct, remaining, fill_price,
    )

    try:
        from yukti.telegram.bot import alert
        next_note = f" | Next target ₹{old_t2:.2f}" if old_t2 else " | Trailing SL active"
        await alert(
            f"🎯 *T1 Hit — {symbol}*\n"
            f"Sold {half_qty} shares @ ₹{exit_price:.2f} | P&L: {pnl_pct:+.2f}%\n"
            f"Holding {remaining} shares | SL → breakeven ₹{fill_price:.2f}{next_note}"
        )
    except Exception:
        pass


async def _update_trailing_sl(
    symbol: str,
    pos: dict[str, Any],
    last_high: float,
    last_low: float,
    is_long: bool,
) -> None:
    """Tighten trailing stop behind the high-water mark. Redis-only update."""
    current = float(pos.get("trailing_sl") or pos.get("stop_loss", 0))
    if is_long:
        candidate = round(last_high * (1 - TRAIL_PCT), 2)
        if candidate <= current:
            return
    else:
        candidate = round(last_low * (1 + TRAIL_PCT), 2)
        if candidate >= current:
            return

    pos["trailing_sl"] = candidate
    r = await get_redis()
    await r.set(f"yukti:positions:{symbol}", json.dumps(pos))
    log.debug("Trailing SL %s: ₹%.2f → ₹%.2f", symbol, current, candidate)


# ═══════════════════════════════════════════════════════════════
#  TRADE JOURNAL
# ═══════════════════════════════════════════════════════════════

async def write_journal_entry(
    trade:              dict[str, Any],
    original_reasoning: str,
) -> "JournalReflection":
    """Write a structured post-trade reflection using Claude.

    Returns a `JournalReflection` model ready to pass to `store_journal`.
    """
    from yukti.agents.journal import write_journal_entry as write_journal_structured
    from yukti.agents.rag_schemas import JournalReflection

    refl = await write_journal_structured(
        symbol      = trade.get("symbol", ""),
        direction   = trade.get("direction", ""),
        setup_type  = trade.get("setup_type", ""),
        entry       = float(trade.get("fill_price") or trade.get("entry_price", 0)),
        stop_loss   = float(trade.get("stop_loss", 0)),
        target      = float(trade.get("target_1", 0)),
        exit_price  = float(trade.get("exit_price", 0)),
        exit_reason = trade.get("exit_reason", ""),
        pnl_pct     = float(trade.get("pnl_pct", 0)),
        conviction  = int(trade.get("conviction", 0)),
        reasoning   = original_reasoning,
        market_bias = trade.get("market_bias", "NEUTRAL"),
    )
    log.info("Journal written for %s (quality=%d)", trade.get("symbol", ""), refl.quality_score or 0)
    return refl


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


from yukti.scheduler.calendar import is_trading_day, is_trading_hours


# ═══════════════════════════════════════════════════════════════
#  SCHEDULER JOBS
# ═══════════════════════════════════════════════════════════════

async def job_morning_prep() -> None:
    """09:00 — reconcile positions, reset daily state."""
    from yukti.execution.reconcile import reconcile_positions
    log.info("=== Morning prep ===")
    await reconcile_positions()



async def job_daily_journal(closed_trades: list[dict[str, Any]]) -> None:
    """16:00 — write journal entries and embed them for memory."""
    from yukti.data.database import get_db
    from yukti.data.models import JournalEntry
    log.info("=== Daily journal: %d trades ===", len(closed_trades))

    for trade in closed_trades:
        if trade.get("pnl") is None:
            continue
        refl = await write_journal_entry(
            trade,
            original_reasoning=trade.get("reasoning", ""),
        )
        from yukti.agents.memory import store_journal
        await store_journal(
            trade_id  = trade.get("db_id", 0),
            symbol    = trade.get("symbol", ""),
            setup_type= trade.get("setup_type", ""),
            direction = trade.get("direction", ""),
            pnl_pct   = float(trade.get("pnl_pct", 0)),
            journal   = refl,
            conviction= int(trade.get("conviction", 5)),
        )
        log.info("Journal saved for %s (quality=%d)", trade.get("symbol", ""), refl.quality_score or 0)
