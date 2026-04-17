"""
yukti/risk/sizing.py  ·  yukti/risk/sl_target.py  ·  yukti/risk/gates.py  ·  yukti/risk/cooldown.py
Combined into one file for brevity. Split into submodules in the actual project.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from yukti.config import settings
from yukti.data.state import (
    count_open_positions,
    get_daily_pnl_pct,
    get_performance_state,
    is_on_cooldown,
)

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  POSITION SIZING
# ═══════════════════════════════════════════════════════════════

@dataclass
class PositionResult:
    quantity:              int
    base_quantity:         int
    conviction_multiplier: float
    risk_amount:           float   # ₹ risked
    stop_distance:         float   # ₹ per share
    max_loss:              float   # ₹ total max loss
    capital_deployed:      float   # ₹ notional
    capital_pct:           float   # % of account deployed


def calculate_position(
    entry_price: float,
    stop_loss:   float,
    direction:   str,
    conviction:  int,
    account_value: float | None = None,
    risk_pct:      float | None = None,
) -> PositionResult:
    """
    ATR / risk-first position sizing with conviction multiplier.

    Formula:
        risk_amount   = account_value × risk_pct
        stop_distance = |entry - stop_loss|
        base_qty      = floor(risk_amount / stop_distance)
        final_qty     = floor(base_qty × conviction_multiplier)

    Conviction → multiplier:
        9-10 → 1.5×   (high confidence — size up)
         7-8 → 1.0×   (standard)
         5-6 → 0.5×   (tentative — half size)
         1-4 → 0.0×   (should have been SKIPped; safe guard)
    """
    acct  = account_value or settings.account_value
    rpct  = risk_pct or settings.risk_pct

    risk_amount = acct * rpct

    stop_dist = (
        entry_price - stop_loss if direction == "LONG"
        else stop_loss - entry_price
    )

    if stop_dist <= 0:
        raise ValueError(
            f"Invalid stop: {direction} entry={entry_price} sl={stop_loss}"
        )

    base_qty = int(risk_amount / stop_dist)

    mult_map = {(9, 10): 1.5, (7, 8): 1.0, (5, 6): 0.5}
    mult = 0.0
    for (lo, hi), m in mult_map.items():
        if lo <= conviction <= hi:
            mult = m
            break

    final_qty   = int(base_qty * mult)
    capital_dep = final_qty * entry_price

    return PositionResult(
        quantity              = final_qty,
        base_quantity         = base_qty,
        conviction_multiplier = mult,
        risk_amount           = round(risk_amount, 2),
        stop_distance         = round(stop_dist, 2),
        max_loss              = round(final_qty * stop_dist, 2),
        capital_deployed      = round(capital_dep, 2),
        capital_pct           = round(capital_dep / acct * 100, 2),
    )


# ═══════════════════════════════════════════════════════════════
#  SL / TARGET CALCULATOR
# ═══════════════════════════════════════════════════════════════

@dataclass
class Levels:
    stop_loss:     float
    stop_distance: float
    target_1:      float
    target_2:      float
    risk_reward:   float
    entry_quality: str    # "GOOD" | "WIDE_STOP"


def calculate_levels(
    direction:   str,
    entry_price: float,
    atr:         float,
    swing_low:   Optional[float] = None,
    swing_high:  Optional[float] = None,
    target_rr:   tuple[float, float] = (2.0, 3.0),
) -> Levels:
    """
    Structural SL + target calculation.

    LONG:
      sl   = max(entry - atr*1.5,  swing_low * 0.995)  ← tighter (higher)
      t1   = entry + 2.0 × stop_dist
      t2   = entry + 3.0 × stop_dist

    SHORT:
      sl   = min(entry + atr*1.5,  swing_high * 1.005) ← tighter (lower)
      t1   = entry - 2.0 × stop_dist
      t2   = entry - 3.0 × stop_dist
    """
    atr_m = settings.atr_multiplier

    if direction == "LONG":
        atr_sl    = entry_price - atr * atr_m
        swing_sl  = swing_low * 0.995  if swing_low  else atr_sl
        sl        = max(atr_sl, swing_sl)
        stop_dist = entry_price - sl
        t1 = round(entry_price + stop_dist * target_rr[0], 2)
        t2 = round(entry_price + stop_dist * target_rr[1], 2)
    else:
        atr_sl    = entry_price + atr * atr_m
        swing_sl  = swing_high * 1.005 if swing_high else atr_sl
        sl        = min(atr_sl, swing_sl)
        stop_dist = sl - entry_price
        t1 = round(entry_price - stop_dist * target_rr[0], 2)
        t2 = round(entry_price - stop_dist * target_rr[1], 2)

    if stop_dist <= 0:
        raise ValueError(f"Computed stop distance is {stop_dist} — bad entry")

    quality = "WIDE_STOP" if stop_dist > atr * settings.max_atr_multiplier else "GOOD"

    return Levels(
        stop_loss     = round(sl, 2),
        stop_distance = round(stop_dist, 2),
        target_1      = t1,
        target_2      = t2,
        risk_reward   = round(target_rr[0], 2),
        entry_quality = quality,
    )


# ═══════════════════════════════════════════════════════════════
#  RISK GATES  — run before every order hits DhanHQ
# ═══════════════════════════════════════════════════════════════

@dataclass
class GateResult:
    passed:  bool
    reason:  str | None = None


async def run_gates(
    symbol:    str,
    direction: str,
    rr:        float,
    position:  PositionResult,
    account_value: float | None = None,
) -> GateResult:
    """
    Run all pre-trade risk checks in order. Return first failure.
    All checks are async because they may read Redis.
    """
    acct = account_value or settings.account_value

    # 1. Daily loss limit
    daily_pnl = await get_daily_pnl_pct()
    if daily_pnl <= -settings.daily_loss_limit_pct:
        return GateResult(False, f"daily_loss_limit: {daily_pnl:.2%} <= -{settings.daily_loss_limit_pct:.2%}")

    # 2. Max open positions
    open_count = await count_open_positions()
    if open_count >= settings.max_open_positions:
        return GateResult(False, f"max_positions: {open_count} >= {settings.max_open_positions}")

    # 3. Symbol cooldown
    if await is_on_cooldown(symbol):
        return GateResult(False, f"cooldown: {symbol} recently traded")

    # 4. R:R minimum
    if rr < settings.min_rr:
        return GateResult(False, f"rr_too_low: {rr:.2f} < {settings.min_rr}")

    # 5. Zero quantity (conviction too low)
    if position.quantity <= 0:
        return GateResult(False, "zero_quantity: conviction too low or stop too tight")

    # 6. Max loss per trade hard cap
    loss_cap = acct * settings.max_loss_cap_pct
    if position.max_loss > loss_cap:
        return GateResult(False, f"max_loss_cap: ₹{position.max_loss:.0f} > ₹{loss_cap:.0f}")

    # 7. Max single-stock concentration
    max_cap = acct * settings.max_single_stock_pct
    if position.capital_deployed > max_cap:
        # Trim quantity instead of rejecting
        trim_qty = int(max_cap / (position.capital_deployed / position.quantity))
        if trim_qty <= 0:
            return GateResult(False, f"concentration_trim_zero_qty: max_cap={max_cap}, deployed={position.capital_deployed}")
        log.warning(
            "Trimming %s qty %d→%d due to concentration limit",
            symbol, position.quantity, trim_qty
        )
        position.quantity         = trim_qty
        position.capital_deployed = round(trim_qty * (position.capital_deployed / position.quantity), 2)
        position.max_loss         = round(trim_qty * position.stop_distance, 2)
        position.capital_pct      = round(position.capital_deployed / acct * 100, 2)

    return GateResult(True)
