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
            log.warning("Stale ARMED for %s — cleaning", symbol)
            await delete_position(symbol)
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
