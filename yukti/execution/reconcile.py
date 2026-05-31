"""
yukti/execution/reconcile.py
Morning reconciliation + startup crash recovery.

Two phases:

Phase A — Crash recovery (runs first, always)
    Detect intents in inconsistent states from a prior crash.
    - PLACED but broker has no pending order → mark abandoned
    - FILLED but no GTTs → check broker, re-arm or market-exit
    - Stuck in PLACED > 10 min → cancel + abandon

Phase B — Daily reconciliation (9:05 IST)
    Compare Redis-tracked positions to DhanHQ actual positions.
    Mismatches halt the agent.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from yukti.data.state import (
    delete_position,
    get_all_positions,
    is_halted,
    set_halt,
    save_position,
    reset_daily_pnl,
)
from yukti.execution.broker_factory import get_broker
from yukti.execution.order_intent import (
    find_unsafe_intents,
    find_stale_intents,
    mark_abandoned,
    mark_armed,
    mark_unsafe,
)

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  PHASE A — CRASH RECOVERY (runs on startup + scheduler)
# ═══════════════════════════════════════════════════════════════

async def recover_from_crash() -> dict[str, int]:
    """
    Scan for intents in dangerous states and recover each one.
    Returns {state: count} breakdown of recovered items.
    Non-fatal — logs and continues on individual failures.
    """
    log.info("=== Crash recovery scan starting ===")
    stats = {
        "stale_cancelled": 0,
        "rearmed": 0,
        "emergency_exit": 0,
        "ghost_abandoned": 0,
        "partial_exiting_recovered": 0,
    }

    # 0. Positions stuck mid-partial-exit. _partial_exit_t1 sets
    #    status=PARTIAL_EXITING before any broker call; if the process died
    #    between cancelling the T1 GTT and arming the new breakeven SL, the
    #    remaining shares may sit naked at the broker. Force-flatten any
    #    quantity the broker still holds for these symbols.
    #
    #    IMPORTANT: reconcile_positions() can be scheduled CONCURRENTLY from
    #    monitor_positions() when an unrelated symbol disappears from the
    #    broker. If we naively force-flatten anything in PARTIAL_EXITING, we
    #    can clobber a live partial exit that's mid-flight (~0.5s window per
    #    call). The partial_exit_started_at timestamp lets us distinguish a
    #    truly stuck position (old) from one in active flight (fresh). Only
    #    act on entries that have been in PARTIAL_EXITING for > 60s.
    PARTIAL_EXIT_STUCK_THRESHOLD_SECS = 60
    all_positions = await get_all_positions()
    for symbol, pos in all_positions.items():
        if pos.get("status") != "PARTIAL_EXITING":
            continue

        started = pos.get("partial_exit_started_at")
        if started:
            try:
                elapsed = (datetime.utcnow() - datetime.fromisoformat(started)).total_seconds()
            except (TypeError, ValueError):
                # Malformed timestamp — treat as old to be safe (the alternative
                # is leaving a possibly-naked position uncovered indefinitely).
                elapsed = float("inf")
            if elapsed < PARTIAL_EXIT_STUCK_THRESHOLD_SECS:
                log.info(
                    "Skipping PARTIAL_EXITING recovery for %s — %ss old, likely in flight",
                    symbol, int(elapsed),
                )
                continue

        log.warning("Position %s stuck in PARTIAL_EXITING — force-flattening", symbol)
        direction    = (pos.get("direction") or "LONG").upper()
        security_id  = pos.get("security_id", "")
        product_type = "INTRADAY" if pos.get("holding_period") == "intraday" else "DELIVERY"

        try:
            broker_positions = await get_broker().get_positions()
        except Exception as exc:
            log.error(
                "PARTIAL_EXITING recovery for %s: cannot fetch broker positions: %s",
                symbol, exc,
            )
            continue

        actual_qty = 0
        for bp in broker_positions:
            if bp.get("tradingSymbol") == symbol:
                actual_qty = abs(int(bp.get("netQty", 0)))
                break

        # Cancel any lingering GTTs (best-effort) so they don't fire after we
        # market-exit. T1 was likely cancelled before the crash; SL may or may
        # not still be live depending on where the crash hit.
        for gtt_field in ("sl_gtt_id", "target_gtt_id"):
            gtt_id = pos.get(gtt_field)
            if gtt_id:
                try:
                    await get_broker().cancel_gtt(gtt_id)
                except Exception:
                    pass

        if actual_qty > 0:
            try:
                await get_broker().market_exit(security_id, direction, actual_qty, product_type)
                log.info(
                    "PARTIAL_EXITING recovery: force-flattened %d shares of %s",
                    actual_qty, symbol,
                )
            except Exception as exc:
                log.critical(
                    "PARTIAL_EXITING recovery: force-flatten FAILED for %s: %s — halting",
                    symbol, exc,
                )
                try:
                    from yukti.telegram.bot import alert
                    await alert(
                        f"🚨🚨 *CRITICAL — {symbol}*\n"
                        f"Position stuck in PARTIAL_EXITING and force-flatten failed.\n"
                        f"*MANUAL INTERVENTION REQUIRED.*"
                    )
                except Exception:
                    pass
                await set_halt(True)
                continue

        intent_id = pos.get("intent_id")
        if intent_id:
            try:
                await mark_abandoned(int(intent_id), "partial_exiting_recovered")
            except Exception as exc:
                log.warning("Could not mark intent #%s abandoned: %s", intent_id, exc)
        await delete_position(symbol)
        stats["partial_exiting_recovered"] += 1
        try:
            from yukti.telegram.bot import alert
            await alert(
                f"♻️ Recovery: {symbol} was stuck mid-partial-exit, "
                f"force-flattened {actual_qty} shares."
            )
        except Exception:
            pass

    # 1. Stale PLACED intents — entry order probably didn't fill
    stale = await find_stale_intents(older_than_minutes=10)
    for intent in stale:
        try:
            if intent.entry_order_id:
                await get_broker().cancel_order(intent.entry_order_id)
            await mark_abandoned(intent.id, "stale_after_restart")
            await delete_position(intent.symbol)
            stats["stale_cancelled"] += 1
            log.info("Recovered intent #%d: cancelled stale order %s",
                     intent.id, intent.entry_order_id)
        except Exception as exc:
            log.error("Failed to recover stale intent #%d: %s", intent.id, exc)

    # 2. FILLED but never ARMED — the critical race condition
    unsafe = await find_unsafe_intents()
    for intent in unsafe:
        if intent.state != "FILLED":
            continue

        log.warning("Intent #%d FILLED but not ARMED — recovering", intent.id)

        # Verify with broker — does the position actually exist?
        position_exists = await _verify_position(intent.symbol, intent.direction, intent.filled_qty)

        if not position_exists:
            # Broker doesn't have it — probably already closed manually or stale
            await mark_abandoned(intent.id, "filled_state_but_no_broker_position")
            await delete_position(intent.symbol)
            stats["ghost_abandoned"] += 1
            log.info("Intent #%d: no broker position found, marked abandoned", intent.id)
            continue

        # Broker has the position — arm GTTs now
        exit_side = "SELL" if intent.direction == "LONG" else "BUY"
        product_type = "INTRADAY" if intent.holding_period == "intraday" else "DELIVERY"

        try:
            sl_gtt = await get_broker().place_gtt(
                security_id      = intent.security_id,
                transaction_type = exit_side,
                quantity         = intent.filled_qty,
                trigger_price    = intent.stop_loss,
                order_type       = "SL-M",
                product_type     = product_type,
            )
            if isinstance(sl_gtt, dict) and str(sl_gtt.get("status", "")).upper() == "ERROR":
                raise RuntimeError(f"sl_gtt_api_error: {sl_gtt.get('message', sl_gtt)}")
            sl_id = sl_gtt.get("gttOrderId") or (sl_gtt.get("data") or {}).get("gttOrderId")
            if not sl_id:
                raise RuntimeError(f"sl_gtt_no_id_returned: {sl_gtt}")

            t1_gtt = await get_broker().place_gtt(
                security_id      = intent.security_id,
                transaction_type = exit_side,
                quantity         = intent.filled_qty,
                trigger_price    = intent.target_1,
                order_type       = "LIMIT",
                product_type     = product_type,
                price            = intent.target_1,
            )
            if isinstance(t1_gtt, dict) and str(t1_gtt.get("status", "")).upper() == "ERROR":
                raise RuntimeError(f"t1_gtt_api_error: {t1_gtt.get('message', t1_gtt)}")
            t1_id = t1_gtt.get("gttOrderId") or (t1_gtt.get("data") or {}).get("gttOrderId")
            if not t1_id:
                raise RuntimeError(f"t1_gtt_no_id_returned: {t1_gtt}")

            await mark_armed(intent.id, sl_id, t1_id)

            # Refresh Redis position state
            pos = {
                "intent_id":      intent.id,
                "symbol":         intent.symbol,
                "security_id":    intent.security_id,
                "direction":      intent.direction,
                "quantity":       intent.filled_qty,
                "entry_price":    intent.entry_price,
                "fill_price":     intent.fill_price,
                "stop_loss":      intent.stop_loss,
                "target_1":       intent.target_1,
                "target_2":       intent.target_2,
                "conviction":     intent.conviction,
                "setup_type":     intent.setup_type,
                "holding_period": intent.holding_period,
                "reasoning":      intent.reasoning,
                "entry_order_id": intent.entry_order_id,
                "sl_gtt_id":      sl_id,
                "target_gtt_id":  t1_id,
                "status":         "ARMED",
            }
            await save_position(intent.symbol, pos)
            stats["rearmed"] += 1
            log.info("Intent #%d RE-ARMED successfully", intent.id)

        except Exception as exc:
            # Can't re-arm GTTs — market exit as safety measure
            log.critical("Cannot re-arm intent #%d — market exiting: %s", intent.id, exc)
            try:
                await get_broker().market_exit(
                    intent.security_id, intent.direction, intent.filled_qty, product_type
                )
                await mark_unsafe(intent.id, f"rearm_failed_market_exit: {exc}")
                await delete_position(intent.symbol)
                stats["emergency_exit"] += 1
                # Alert
                try:
                    from yukti.telegram.bot import alert
                    await alert(
                        f"🚨 Recovery: could not re-arm intent #{intent.id} for {intent.symbol}. "
                        f"Market-exit executed."
                    )
                except Exception:
                    pass
            except Exception as exit_exc:
                log.critical(
                    "Could not market-exit intent #%d either: %s. MANUAL INTERVENTION NEEDED.",
                    intent.id, exit_exc,
                )
                try:
                    from yukti.telegram.bot import alert
                    await alert(
                        f"🚨🚨 CRITICAL: intent #{intent.id} {intent.symbol} "
                        f"cannot be recovered or exited. MANUAL ACTION REQUIRED."
                    )
                except Exception:
                    pass
                await set_halt(True)

    log.info("=== Recovery complete: %s ===", stats)
    return stats


async def _verify_position(symbol: str, direction: str, expected_qty: int) -> bool:
    """Check if DhanHQ actually has this position."""
    try:
        broker_positions = await get_broker().get_positions()
    except Exception as exc:
        log.warning("Cannot verify position (broker unreachable): %s", exc)
        return False   # Fail-safe: assume not present

    for bp in broker_positions:
        if bp.get("tradingSymbol") != symbol:
            continue
        net_qty = int(bp.get("netQty", 0))
        if direction == "LONG"  and net_qty >=  expected_qty * 0.9:
            return True
        if direction == "SHORT" and net_qty <= -expected_qty * 0.9:
            return True

    return False


async def _close_stale_armed(symbol: str, pos: dict[str, Any]) -> None:
    """
    The broker no longer holds shares for `symbol` but our state still says
    ARMED — the SL or T1 GTT order fired. Identify which leg filled, fetch
    its fill price, route through close_trade() so P&L is recorded, then
    cancel whichever leg is still open so it can't re-enter the trade.
    """
    from yukti.execution.order_sm import close_trade
    sl_id   = pos.get("sl_gtt_id")
    tgt_id  = pos.get("target_gtt_id")
    entry   = float(pos.get("fill_price") or pos.get("entry_price") or 0.0)
    is_long = (pos.get("direction") or "").upper() == "LONG"
    target  = float(pos.get("target_1") or 0.0)
    stop    = float(pos.get("stop_loss") or 0.0)

    broker = get_broker()
    exit_price: float | None = None
    exit_reason = "broker_exit_reconciled"
    filled_leg_id: str | None = None
    other_leg_id: str | None = None

    async def _try_status(oid: str) -> tuple[bool, float]:
        """Return (filled, avg_price). Tolerant to errors."""
        try:
            st = await broker.get_order_status(oid)
            data = st.get("data", st) if isinstance(st, dict) else {}
            if isinstance(data, list):
                data = data[0] if data else {}
            raw_status = str(data.get("orderStatus") or data.get("status") or "").upper()
            avg = float(data.get("averagePrice") or data.get("avgPrice") or 0.0)
            filled = raw_status in ("TRADED", "FILLED", "COMPLETE") or avg > 0
            return filled, avg
        except Exception as exc:
            log.debug("Stale-ARMED: get_order_status(%s) failed: %s", oid, exc)
            return False, 0.0

    if sl_id:
        sl_filled, sl_avg = await _try_status(sl_id)
        if sl_filled:
            exit_price = sl_avg or stop
            exit_reason = "stop_loss_hit_reconciled"
            filled_leg_id = sl_id
            other_leg_id  = tgt_id
    if exit_price is None and tgt_id:
        tg_filled, tg_avg = await _try_status(tgt_id)
        if tg_filled:
            exit_price = tg_avg or target
            exit_reason = "target_2_hit_reconciled"  # full close, not partial
            filled_leg_id = tgt_id
            other_leg_id  = sl_id

    # Fallback: order-status lookups didn't tell us which leg fired.
    # Infer from the latest LTP — if it's beyond SL, treat as SL hit; if
    # beyond target, treat as target hit; otherwise mark as broker_exit.
    if exit_price is None:
        try:
            sec = pos.get("security_id", "")
            candles = await broker.get_candles(sec, interval="1") if sec else []
            ltp = float(candles[-1].get("close", 0)) if candles else 0.0
        except Exception:
            ltp = 0.0
        if ltp > 0 and stop > 0:
            hit_sl = (ltp <= stop) if is_long else (ltp >= stop)
            hit_tgt = (target > 0 and ((ltp >= target) if is_long else (ltp <= target)))
            if hit_sl:
                exit_price = stop
                exit_reason = "stop_loss_hit_reconciled"
            elif hit_tgt:
                exit_price = target
                exit_reason = "target_2_hit_reconciled"
            else:
                exit_price = ltp
                exit_reason = "broker_exit_reconciled"
        else:
            # No price signal available — last resort: use entry so the
            # position is at least closed and the intent is marked done.
            # P&L=0 but logged loudly so it shows up in journal review.
            exit_price = entry or stop or target
            exit_reason = "broker_exit_reconciled_no_price"
            log.error(
                "Stale-ARMED %s: could not determine exit price (sl_id=%s tgt_id=%s) "
                "— using ₹%.2f as best-effort fallback. P&L will be inaccurate.",
                symbol, sl_id, tgt_id, exit_price,
            )

    # Cancel the leg that did NOT fire so it can't accidentally enter a new
    # position later in the day (regular SL / LIMIT orders persist till EOD).
    if other_leg_id:
        try:
            await broker.cancel_order(other_leg_id)
            log.info("Stale-ARMED %s: cancelled dangling leg %s", symbol, other_leg_id)
        except Exception as exc:
            log.warning("Stale-ARMED %s: failed to cancel dangling leg %s: %s",
                        symbol, other_leg_id, exc)

    log.warning(
        "Stale ARMED for %s — closing properly @ ₹%.2f (reason=%s, filled_leg=%s)",
        symbol, exit_price, exit_reason, filled_leg_id,
    )
    try:
        await close_trade(symbol, exit_price, exit_reason)
    except Exception as exc:
        # close_trade() failed — fall back to the old behaviour so we don't
        # leave a stale position blocking reconciliation forever.
        log.error("Stale-ARMED %s: close_trade failed (%s) — deleting position",
                  symbol, exc)
        await delete_position(symbol)


# ═══════════════════════════════════════════════════════════════
#  PHASE B — DAILY RECONCILIATION (unchanged)
# ═══════════════════════════════════════════════════════════════

async def reconcile_positions() -> bool:
    """
    Morning reconciliation: compare Redis positions vs DhanHQ broker.
    Halt the agent on significant mismatch.
    """
    # Always run crash recovery first
    await recover_from_crash()

    # Reset daily P&L only before market opens (before 09:15 IST).
    # Resetting mid-day on a crash-restart would wipe morning losses and
    # let the agent bypass the daily loss-limit gate for the rest of the day.
    from datetime import datetime, time as dt_time
    from yukti.scheduler.calendar import KOLKATA
    now_ist = datetime.now(KOLKATA).time()
    if now_ist < dt_time(9, 15):
        await reset_daily_pnl()

    redis_positions: dict[str, Any] = await get_all_positions()

    try:
        broker_positions_raw: list[dict] = await get_broker().get_positions()
    except Exception as exc:
        log.error("Failed to fetch broker positions: %s", exc)
        return True

    broker_map: dict[str, int] = {}
    for bp in broker_positions_raw:
        symbol  = bp.get("tradingSymbol", "")
        net_qty = int(bp.get("netQty", 0))
        if net_qty != 0:
            broker_map[symbol] = net_qty

    mismatches: list[str] = []

    for symbol, pos in redis_positions.items():
        expected_qty = int(pos.get("quantity", 0))
        actual_qty   = broker_map.get(symbol, 0)

        if pos.get("status") in ("PLACED", "PLANNED", "CANCELLED", "ABANDONED"):
            continue

        if actual_qty == 0 and pos.get("status") == "ARMED":
            # Broker SL or T1 fired and closed the position; we still hold
            # ARMED in Redis. Don't silently drop — figure out which leg
            # filled, reconstruct the exit price, record the P&L, and cancel
            # the leg that didn't fire so it can't accidentally re-enter.
            await _close_stale_armed(symbol, pos)
            continue

        qty_diff_pct = abs(expected_qty - abs(actual_qty)) / max(expected_qty, 1)
        if qty_diff_pct > 0.10:
            mismatches.append(f"{symbol}: Redis={expected_qty} broker={actual_qty}")

    for symbol, qty in broker_map.items():
        if symbol not in redis_positions:
            mismatches.append(f"GHOST: {symbol} in broker qty={qty}, not in Redis")

    if mismatches:
        log.critical("RECONCILIATION FAILED — halting:\n%s", "\n".join(mismatches))
        await set_halt(True)
        try:
            from yukti.telegram.bot import alert
            await alert(
                "🛑 *Reconciliation FAILED*\n" + "\n".join(mismatches[:5]) +
                "\n\nAgent halted. Manual review needed."
            )
        except Exception:
            pass
        return False

    log.info("Reconciliation OK (Redis=%d, broker=%d)",
             len(redis_positions), len(broker_map))
    return True
