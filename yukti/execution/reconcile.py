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
from yukti.execution.dhan_client import dhan
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
    stats = {"stale_cancelled": 0, "rearmed": 0, "emergency_exit": 0, "ghost_abandoned": 0}

    # 1. Stale PLACED intents — entry order probably didn't fill
    stale = await find_stale_intents(older_than_minutes=10)
    for intent in stale:
        try:
            if intent.entry_order_id:
                await dhan.cancel_order(intent.entry_order_id)
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
            sl_gtt = await dhan.place_gtt(
                security_id      = intent.security_id,
                transaction_type = exit_side,
                quantity         = intent.filled_qty,
                trigger_price    = intent.stop_loss,
                order_type       = "SL-M",
                product_type     = product_type,
            )
            sl_id = sl_gtt.get("gttOrderId") or sl_gtt.get("data", {}).get("gttOrderId")

            t1_gtt = await dhan.place_gtt(
                security_id      = intent.security_id,
                transaction_type = exit_side,
                quantity         = intent.filled_qty,
                trigger_price    = intent.target_1,
                order_type       = "LIMIT",
                product_type     = product_type,
                price            = intent.target_1,
            )
            t1_id = t1_gtt.get("gttOrderId") or t1_gtt.get("data", {}).get("gttOrderId")

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
                await dhan.market_exit(
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
        broker_positions = await dhan.get_positions()
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

    # Reset daily state
    await reset_daily_pnl()

    redis_positions: dict[str, Any] = await get_all_positions()

    try:
        broker_positions_raw: list[dict] = await dhan.get_positions()
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
