"""
yukti/execution/order_sm.py
Crash-safe order state machine.

Key property: At every awaitable boundary, the system state is recoverable
from Postgres alone. If the process dies between any two awaits, the morning
reconciliation job will detect the inconsistency and either:
  - Re-arm missing GTTs for a filled position
  - Market-exit a FILLED-but-UNSAFE position
  - Mark a PLACED-but-unfilled entry as ABANDONED

The sequence with persistence checkpoints:
  1. save_intent(PLANNED)            ← persisted BEFORE any DhanHQ call
  2. place_entry → mark_placed(PLACED)
  3. poll for fill
  4. mark_filled(FILLED)             ← position exists in broker, not protected
  5. arm SL GTT
  6. arm target GTT
  7. mark_armed(ARMED)               ← fully protected

  If 5 or 6 fails → mark_unsafe() → immediate market-exit
  If crash after 4 before 7 → startup recovery re-attempts 5+6
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from yukti.agents.arjun import TradeDecision
from yukti.data.state import (
    delete_position,
    get_position,
    save_position,
    set_cooldown,
    add_to_daily_pnl,
    record_trade_outcome,
    increment_trades_today,
)
from yukti.execution.dhan_client import dhan
from yukti.execution.order_intent import (
    save_intent,
    mark_placed,
    mark_filled,
    mark_armed,
    mark_closed,
    mark_unsafe,
    mark_abandoned,
)
from yukti.risk import PositionResult

log = logging.getLogger(__name__)

FILL_POLL_SECS    = 5
FILL_TIMEOUT_SECS = 120


async def open_trade(
    symbol:       str,
    security_id:  str,
    decision:     TradeDecision,
    position:     PositionResult,
) -> dict[str, Any] | None:
    """
    Crash-safe trade opener.
    Returns position dict or None on any failure.
    """
    is_long      = decision.direction == "LONG"
    intraday     = decision.holding_period == "intraday"
    product_type = "INTRADAY" if intraday else "DELIVERY"
    entry_side   = "BUY" if is_long else "SELL"

    # ═══════════════════════════════════════════════════════════
    #  STEP 1 — Save intent to Postgres FIRST (before any DhanHQ call)
    #  If anything after this crashes, startup recovery handles it.
    # ═══════════════════════════════════════════════════════════
    try:
        intent_id = await save_intent(
            symbol         = symbol,
            security_id    = security_id,
            direction      = decision.direction or "LONG",
            holding_period = decision.holding_period,
            quantity       = position.quantity,
            entry_price    = decision.entry_price or 0.0,
            stop_loss      = decision.stop_loss or 0.0,
            target_1       = decision.target_1 or 0.0,
            target_2       = decision.target_2,
            conviction     = decision.conviction,
            setup_type     = decision.setup_type or "unknown",
            reasoning      = decision.reasoning,
        )
    except Exception as exc:
        log.error("Failed to save intent for %s: %s", symbol, exc)
        return None

    # ═══════════════════════════════════════════════════════════
    #  STEP 2 — Place entry order
    # ═══════════════════════════════════════════════════════════
    try:
        order_resp = await dhan.place_order(
            security_id      = security_id,
            transaction_type = entry_side,
            quantity         = position.quantity,
            order_type       = "LIMIT" if decision.entry_type != "MARKET" else "MARKET",
            product_type     = product_type,
            price            = decision.entry_price or 0.0,
            tag              = f"yukti-{decision.setup_type or 'trade'}",
        )
    except Exception as exc:
        await mark_abandoned(intent_id, f"entry_order_failed: {exc}")
        log.error("Entry order failed for %s (intent #%d): %s", symbol, intent_id, exc)
        return None

    order_id = order_resp.get("orderId") or order_resp.get("data", {}).get("orderId")
    if not order_id:
        await mark_abandoned(intent_id, f"no_order_id_in_response: {order_resp}")
        return None

    await mark_placed(intent_id, order_id)

    pos: dict[str, Any] = {
        "intent_id":      intent_id,
        "symbol":         symbol,
        "security_id":    security_id,
        "direction":      decision.direction,
        "setup_type":     decision.setup_type,
        "holding_period": decision.holding_period,
        "entry_price":    decision.entry_price,
        "stop_loss":      decision.stop_loss,
        "target_1":       decision.target_1,
        "target_2":       decision.target_2,
        "quantity":       position.quantity,
        "conviction":     decision.conviction,
        "risk_reward":    decision.risk_reward,
        "reasoning":      decision.reasoning,
        "entry_order_id": order_id,
        "status":         "PLACED",
    }
    await save_position(symbol, pos)
    await increment_trades_today()

    # ═══════════════════════════════════════════════════════════
    #  STEP 3 — Poll for fill
    # ═══════════════════════════════════════════════════════════
    fill_price, filled_qty = await _wait_for_fill(
        order_id,
        expected_qty = position.quantity,
        timeout_secs = FILL_TIMEOUT_SECS,
    )

    if filled_qty == 0:
        # Cancel and mark abandoned
        try:
            await dhan.cancel_order(order_id)
        except Exception:
            pass
        await mark_abandoned(intent_id, "never_filled_cancelled")
        await delete_position(symbol)
        log.info("Entry %s not filled in %ds — cancelled", symbol, FILL_TIMEOUT_SECS)
        return None

    # Handle partial fill
    if filled_qty < position.quantity:
        log.warning("Partial fill %s: %d/%d", symbol, filled_qty, position.quantity)
        pos["quantity"] = filled_qty

    await mark_filled(intent_id, fill_price, filled_qty)
    pos["fill_price"] = fill_price
    pos["status"]     = "FILLED"
    await save_position(symbol, pos)

    # ═══════════════════════════════════════════════════════════
    #  STEP 4 — Arm SL + target GTTs
    #  Critical: if SL fails, immediate market-exit
    # ═══════════════════════════════════════════════════════════
    armed_ok, sl_id, t1_id, err = await _arm_gtts(
        security_id     = security_id,
        direction       = decision.direction or "LONG",
        quantity        = filled_qty,
        stop_loss       = decision.stop_loss or 0.0,
        target_1        = decision.target_1,
        product_type    = product_type,
    )

    if not armed_ok:
        # UNSAFE state — mark it, market-exit, alert
        await mark_unsafe(intent_id, f"gtt_arm_failed: {err}")
        log.critical("UNSAFE: %s filled but GTTs failed — market exiting: %s", symbol, err)
        try:
            await dhan.market_exit(security_id, decision.direction or "LONG", filled_qty, product_type)
            await close_trade(symbol, fill_price, "emergency_exit_gtt_failed")
        except Exception as exit_exc:
            log.critical("CRITICAL: market-exit also failed for %s: %s", symbol, exit_exc)
            try:
                from yukti.telegram.bot import alert
                await alert(
                    f"🚨 *CRITICAL*: {symbol} filled but GTTs + market-exit both failed. "
                    f"MANUAL INTERVENTION REQUIRED. intent #{intent_id}"
                )
            except Exception:
                pass
        return None

    await mark_armed(intent_id, sl_id, t1_id)
    pos["sl_gtt_id"]     = sl_id
    pos["target_gtt_id"] = t1_id
    pos["status"]        = "ARMED"
    await save_position(symbol, pos)

    log.info(
        "Trade ARMED intent #%d: %s %s %d @ ₹%.2f | SL ₹%.2f | T1 ₹%.2f",
        intent_id, decision.direction, symbol, filled_qty,
        fill_price, decision.stop_loss, decision.target_1 or 0,
    )
    return pos


# ═══════════════════════════════════════════════════════════════
#  Fill polling
# ═══════════════════════════════════════════════════════════════

async def _wait_for_fill(
    order_id:     str,
    expected_qty: int,
    timeout_secs: int,
) -> tuple[float, int]:
    """Poll order status until filled, cancelled, or timeout. Returns (fill_price, filled_qty)."""
    elapsed = 0
    while elapsed < timeout_secs:
        await asyncio.sleep(FILL_POLL_SECS)
        elapsed += FILL_POLL_SECS

        try:
            status_resp = await dhan.get_order_status(order_id)
            data        = status_resp.get("data", status_resp)
            order_status = data.get("orderStatus", "")
            filled_qty   = int(data.get("filledQty", 0))
            fill_price   = float(data.get("averagePrice", 0) or 0)
        except Exception as exc:
            log.warning("Order status poll error %s: %s", order_id, exc)
            continue

        if order_status in ("TRADED", "PART_TRADED") and filled_qty >= expected_qty:
            return fill_price, filled_qty
        if order_status in ("REJECTED", "CANCELLED"):
            return 0.0, 0
        if order_status == "PART_TRADED":
            # Partial fill, keep polling for more
            continue

    # Timeout — return whatever we have
    try:
        status_resp = await dhan.get_order_status(order_id)
        data         = status_resp.get("data", status_resp)
        return float(data.get("averagePrice", 0) or 0), int(data.get("filledQty", 0))
    except Exception:
        return 0.0, 0


# ═══════════════════════════════════════════════════════════════
#  GTT arming with retry
# ═══════════════════════════════════════════════════════════════

async def _arm_gtts(
    security_id:   str,
    direction:     str,
    quantity:      int,
    stop_loss:     float,
    target_1:      float | None,
    product_type:  str,
) -> tuple[bool, str, str | None, str | None]:
    """
    Arm SL GTT + target GTT. Returns (success, sl_id, target_id, error).
    SL must succeed. Target is best-effort.
    """
    exit_side = "SELL" if direction == "LONG" else "BUY"

    # SL GTT — MUST succeed. Retry internally via dhan_client tenacity.
    try:
        gtt_sl    = await dhan.place_gtt(
            security_id      = security_id,
            transaction_type = exit_side,
            quantity         = quantity,
            trigger_price    = stop_loss,
            order_type       = "SL-M",
            product_type     = product_type,
        )
        sl_id = gtt_sl.get("gttOrderId") or gtt_sl.get("data", {}).get("gttOrderId")
        if not sl_id:
            return False, "", None, "sl_gtt_no_id_returned"
    except Exception as exc:
        return False, "", None, f"sl_gtt_failed: {exc}"

    # Target GTT — best-effort (monitor will close on target hit if this fails)
    t1_id: str | None = None
    if target_1:
        try:
            gtt_t1 = await dhan.place_gtt(
                security_id      = security_id,
                transaction_type = exit_side,
                quantity         = quantity,
                trigger_price    = target_1,
                order_type       = "LIMIT",
                product_type     = product_type,
                price            = target_1,
            )
            t1_id = gtt_t1.get("gttOrderId") or gtt_t1.get("data", {}).get("gttOrderId")
        except Exception as exc:
            log.warning("Target GTT failed (non-fatal): %s", exc)

    return True, sl_id, t1_id, None


# ═══════════════════════════════════════════════════════════════
#  CLOSE TRADE — unchanged except now marks intent closed
# ═══════════════════════════════════════════════════════════════

async def close_trade(
    symbol:      str,
    exit_price:  float,
    exit_reason: str,
) -> dict[str, Any] | None:
    pos = await get_position(symbol)
    if not pos:
        log.warning("close_trade: no position found for %s", symbol)
        return None

    entry  = float(pos.get("fill_price") or pos.get("entry_price", 0))
    qty    = int(pos.get("quantity", 0))
    is_long = pos.get("direction") == "LONG"

    pnl     = (exit_price - entry) * qty if is_long else (entry - exit_price) * qty
    pnl_pct = pnl / (entry * qty) * 100 if entry * qty else 0.0

    pos["exit_price"]  = exit_price
    pos["exit_reason"] = exit_reason
    pos["pnl"]         = round(pnl, 2)
    pos["pnl_pct"]     = round(pnl_pct, 4)
    pos["status"]      = "SQUAREDOFF" if "eod" in exit_reason or "squareoff" in exit_reason else "CLOSED"
    pos["closed_at"]   = datetime.utcnow().isoformat()

    # Mark intent closed in Postgres
    intent_id = pos.get("intent_id")
    if intent_id:
        try:
            await mark_closed(int(intent_id))
        except Exception as exc:
            log.warning("Failed to mark intent #%s closed: %s", intent_id, exc)

    # Performance state
    await add_to_daily_pnl(pnl_pct)
    await record_trade_outcome(won=pnl > 0)

    # Cancel the other GTT
    if exit_reason == "stop_loss_hit" and pos.get("target_gtt_id"):
        try:
            await dhan.cancel_gtt(pos["target_gtt_id"])
        except Exception:
            pass
    elif "target" in exit_reason and pos.get("sl_gtt_id"):
        try:
            await dhan.cancel_gtt(pos["sl_gtt_id"])
        except Exception:
            pass

    await set_cooldown(symbol)
    await delete_position(symbol)

    log.info(
        "Trade CLOSED: %s %s P&L=₹%.0f (%.2f%%) reason=%s",
        pos.get("direction"), symbol, pnl, pnl_pct, exit_reason
    )
    return pos
