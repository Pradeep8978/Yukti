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
from yukti.execution.broker_factory import get_broker
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
        broker = get_broker()
        order_resp = await broker.place_order(
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
        try:
            from yukti.telegram.bot import alert_order_failed
            await alert_order_failed(symbol, entry_side, str(exc))
        except Exception:
            pass
        return None
    # Defensive handling: some Dhan responses use an envelope {status: 'ERROR', message: ...}
    if isinstance(order_resp, dict) and str(order_resp.get("status", "")).upper() == "ERROR":
        err_msg = order_resp.get("message", str(order_resp))
        await mark_abandoned(intent_id, f"entry_order_api_error: {err_msg}")
        log.error("Entry order API returned error for %s: %s", symbol, order_resp)
        try:
            from yukti.telegram.bot import alert_order_failed
            await alert_order_failed(symbol, entry_side, err_msg)
        except Exception:
            pass
        return None

    order_id = None
    if isinstance(order_resp, dict):
        order_id = order_resp.get("orderId") or (order_resp.get("data") or {}).get("orderId")
    else:
        # Some clients may return non-dict results; best-effort extraction
        try:
            order_id = getattr(order_resp, "orderId", None)
        except Exception:
            order_id = None
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
            await get_broker().cancel_order(order_id)
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

    # Slippage vs. intended entry price: positive = adverse, negative = favourable.
    intended = decision.entry_price or 0.0
    if intended > 0 and fill_price > 0:
        if (decision.direction or "LONG") == "LONG":
            pos["slippage_pct"] = round((fill_price - intended) / intended * 100, 4)
        else:
            pos["slippage_pct"] = round((intended - fill_price) / intended * 100, 4)
        log.info("Slippage %s: %.4f%% (intended ₹%.2f → filled ₹%.2f)",
                 symbol, pos["slippage_pct"], intended, fill_price)

    # Rebase SL/T1 to actual fill, preserving the *distance* the strategy
    # planned. The bot computes stop_loss / target_1 from the planned entry
    # price; if the limit fills better/worse, those absolute prices become
    # tighter or looser relative to where we actually are. Anchor stops to
    # reality so the planned R:R is what gets traded.
    planned_entry = decision.entry_price or 0.0
    rebased_sl    = decision.stop_loss
    rebased_t1    = decision.target_1
    rebased_t2    = decision.target_2
    is_long       = (decision.direction or "LONG") == "LONG"
    if planned_entry > 0 and fill_price > 0 and decision.stop_loss:
        sl_dist = abs(planned_entry - decision.stop_loss)
        t1_dist = abs(decision.target_1 - planned_entry) if decision.target_1 else None
        t2_dist = abs(decision.target_2 - planned_entry) if decision.target_2 else None
        if is_long:
            rebased_sl = round(fill_price - sl_dist, 2)
            rebased_t1 = round(fill_price + t1_dist, 2) if t1_dist is not None else None
            rebased_t2 = round(fill_price + t2_dist, 2) if t2_dist is not None else None
        else:
            rebased_sl = round(fill_price + sl_dist, 2)
            rebased_t1 = round(fill_price - t1_dist, 2) if t1_dist is not None else None
            rebased_t2 = round(fill_price - t2_dist, 2) if t2_dist is not None else None

        if rebased_sl != decision.stop_loss or rebased_t1 != decision.target_1:
            log.info(
                "REBASED %s | fill=₹%.2f planned_entry=₹%.2f | "
                "SL ₹%.2f→₹%.2f (dist ₹%.2f) | T1 %s→%s",
                symbol, fill_price, planned_entry,
                decision.stop_loss, rebased_sl, sl_dist,
                f"₹{decision.target_1:.2f}" if decision.target_1 else "-",
                f"₹{rebased_t1:.2f}" if rebased_t1 else "-",
            )
            pos["stop_loss"] = rebased_sl
            pos["target_1"]  = rebased_t1
            pos["target_2"]  = rebased_t2

    await save_position(symbol, pos)
    # NOTE: increment_trades_today() moved to after ARMED — emergency-exited
    # trades should not consume the daily trade limit.

    # ═══════════════════════════════════════════════════════════
    #  STEP 4 — Arm SL + target GTTs
    #  Critical: if SL fails, immediate market-exit
    # ═══════════════════════════════════════════════════════════
    armed_ok, sl_id, t1_id, err = await _arm_gtts(
        security_id     = security_id,
        direction       = decision.direction or "LONG",
        quantity        = filled_qty,
        stop_loss       = rebased_sl or 0.0,
        target_1        = rebased_t1,
        product_type    = product_type,
    )

    if not armed_ok:
        # UNSAFE state — mark it, market-exit, alert
        await mark_unsafe(intent_id, f"gtt_arm_failed: {err}")
        log.critical("UNSAFE: %s filled but GTTs failed — market exiting: %s", symbol, err)
        try:
            await get_broker().market_exit(security_id, decision.direction or "LONG", filled_qty, product_type)
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
    await increment_trades_today()

    log.info(
        "Trade ARMED intent #%d: %s %s %d @ ₹%.2f | SL ₹%.2f | T1 ₹%.2f",
        intent_id, decision.direction, symbol, filled_qty,
        fill_price, rebased_sl or 0.0, rebased_t1 or 0,
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
            status_resp = await get_broker().get_order_status(order_id)
            data        = status_resp.get("data", status_resp)
            # DhanHQ v2 wraps the order in a 1-element list; older paths /
            # other brokers may return the dict directly. Normalize either.
            if isinstance(data, list):
                data = data[0] if data else {}
            order_status = data.get("orderStatus", "")
            filled_qty   = int(data.get("filledQty", 0))
            fill_price   = float(data.get("averageTradedPrice", data.get("averagePrice", 0)) or 0)
        except Exception as exc:
            log.warning("Order status poll error %s: %s", order_id, exc)
            continue

        log.info(
            "FILL POLL %s | elapsed=%ds status=%s filled=%d/%d price=%.2f",
            order_id, elapsed, order_status, filled_qty, expected_qty, fill_price,
        )

        if order_status in ("TRADED", "PART_TRADED") and filled_qty >= expected_qty:
            return fill_price, filled_qty
        if order_status in ("REJECTED", "CANCELLED"):
            return 0.0, 0
        if order_status == "PART_TRADED":
            # Partial fill, keep polling for more
            continue

    # Timeout — return whatever we have
    try:
        status_resp = await get_broker().get_order_status(order_id)
        data         = status_resp.get("data", status_resp)
        if isinstance(data, list):
            data = data[0] if data else {}
        return (
            float(data.get("averageTradedPrice", data.get("averagePrice", 0)) or 0),
            int(data.get("filledQty", 0)),
        )
    except Exception:
        return 0.0, 0


# ═══════════════════════════════════════════════════════════════
#  GTT arming with retry
# ═══════════════════════════════════════════════════════════════

def _round_to_tick(price: float, tick: float = 0.10) -> float:
    """Round price to nearest NSE tick.

    Default 0.10: a superset of 0.05 (every 0.10 multiple is also a 0.05
    multiple), so it's always exchange-valid and avoids the
    async-rejection trap where DhanHQ accepts a 0.05-precise order at the
    gateway but the exchange rejects it later for stocks like ASTRAL /
    DEEPAKFERT that use 0.10 ticks. Trade-off: SL/T1 prices for 0.05-tick
    stocks may sit up to ₹0.05 off the intended level — accepted as the
    cost of universal validity until per-symbol tick lookup is added.
    """
    return round(round(price / tick) * tick, 2)




def _extract_order_id(resp: Any) -> str | None:
    """Pull the orderId out of DhanHQ's response envelope, tolerant to shape."""
    if not isinstance(resp, dict):
        return None
    data = resp.get("data")
    if isinstance(data, dict):
        return data.get("orderId") or resp.get("orderId")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("orderId")
    return resp.get("orderId")


async def _arm_gtts(
    security_id:   str,
    direction:     str,
    quantity:      int,
    stop_loss:     float,
    target_1:      float | None,
    product_type:  str,
) -> tuple[bool, str, str | None, str | None]:
    """
    Arm SL + target exit orders. Returns (success, sl_id, target_id, error).

    Implementation note: DhanHQ's "Forever" GTT endpoint is for delivery
    (CNC) only — it rejects INTRADAY product. For intraday positions we
    place two regular resting orders instead:
      - SL  : STOP_LOSS (limit) — trigger at stop_loss, limit at trigger+buffer
      - T1  : LIMIT             — at target price

    The OCO link (cancel surviving leg when the other fills) is handled by
    monitor.py / close_trade(), which already calls cancel_gtt(id).

    SL must succeed. Target is best-effort. Prices are tick-rounded.
    """
    exit_side = "SELL" if direction == "LONG" else "BUY"

    # Tick-round to ₹0.10 (always exchange-valid; see _round_to_tick doc).
    # For a BUY stop on a SHORT the limit must be ABOVE trigger; for a SELL
    # stop on a LONG it must be BELOW. 0.5% buffer ensures fast fill on
    # activation without inviting slippage panic.
    sl_trigger = _round_to_tick(stop_loss)
    if exit_side == "BUY":
        sl_limit = _round_to_tick(sl_trigger * 1.005)
    else:
        sl_limit = _round_to_tick(sl_trigger * 0.995)

    try:
        sl_resp = await get_broker().place_order(
            security_id      = security_id,
            transaction_type = exit_side,
            quantity         = quantity,
            order_type       = "STOP_LOSS",
            product_type     = product_type,
            price            = sl_limit,
            trigger_price    = sl_trigger,
            tag              = "sl",
        )
    except Exception as exc:
        return False, "", None, f"sl_failed: {exc}"

    if isinstance(sl_resp, dict) and str(sl_resp.get("status", "")).lower() == "failure":
        return False, "", None, f"sl_api_error: {sl_resp.get('remarks', sl_resp)}"

    sl_id = _extract_order_id(sl_resp)
    if not sl_id:
        log.error("SL response had no orderId — payload: %r", sl_resp)
        return False, "", None, "sl_no_id_returned"
    log.info(
        "SL %s %d @ trigger=₹%.2f limit=₹%.2f → id=%s",
        exit_side, quantity, sl_trigger, sl_limit, sl_id,
    )

    # Target — best-effort.
    t1_id: str | None = None
    if target_1:
        t1_price = _round_to_tick(target_1)
        try:
            t1_resp = await get_broker().place_order(
                security_id      = security_id,
                transaction_type = exit_side,
                quantity         = quantity,
                order_type       = "LIMIT",
                product_type     = product_type,
                price            = t1_price,
                trigger_price    = 0.0,
                tag              = "t1",
            )
        except Exception as exc:
            log.warning("Target failed (non-fatal): %s", exc)
            t1_resp = None

        if isinstance(t1_resp, dict) and str(t1_resp.get("status", "")).lower() == "failure":
            log.warning("Target API error (non-fatal): %s", t1_resp)
            t1_resp = None

        if t1_resp:
            t1_id = _extract_order_id(t1_resp)
            if t1_id:
                log.info("T1 %s %d @ ₹%.2f → id=%s", exit_side, quantity, t1_price, t1_id)
            else:
                log.warning("T1 response had no orderId — payload: %r", t1_resp)

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
    is_long = (pos.get("direction") or "").upper() == "LONG"

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
            await get_broker().cancel_gtt(pos["target_gtt_id"])
        except Exception:
            pass
    elif "target" in exit_reason and pos.get("sl_gtt_id"):
        try:
            await get_broker().cancel_gtt(pos["sl_gtt_id"])
        except Exception:
            pass

    await set_cooldown(symbol, conviction=pos.get("conviction"))
    await delete_position(symbol)

    log.info(
        "Trade CLOSED: %s %s P&L=₹%.0f (%.2f%%) reason=%s",
        pos.get("direction"), symbol, pnl, pnl_pct, exit_reason
    )
    return pos
