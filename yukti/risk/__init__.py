"""
yukti/risk/sizing.py  ·  yukti/risk/sl_target.py  ·  yukti/risk/gates.py  ·  yukti/risk/cooldown.py
Combined into one file for brevity. Split into submodules in the actual project.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP

from yukti.agents.arjun import TradeDecision
from yukti.config import settings
from yukti.data.state import is_on_cooldown

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  POSITION SIZING
# ═══════════════════════════════════════════════════════════════

@dataclass
class PositionResult:
    quantity:              int
    base_quantity:         int
    conviction_multiplier: float
    risk_amount:           Decimal   # ₹ risked
    stop_distance:         Decimal   # ₹ per share
    max_loss:              Decimal   # ₹ total max loss
    capital_deployed:      Decimal   # ₹ notional
    capital_pct:           Decimal   # % of account deployed


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
    acct  = Decimal(str(account_value or settings.account_value))
    rpct  = Decimal(str(risk_pct or settings.risk_pct))

    risk_amount = acct * rpct

    entry_d = Decimal(str(entry_price))
    stop_d  = Decimal(str(stop_loss))
    stop_dist = (
        entry_d - stop_d if direction == "LONG"
        else stop_d - entry_d
    )

    if stop_dist <= Decimal("0"):
        raise ValueError(
            f"Invalid stop: {direction} entry={entry_price} sl={stop_loss}"
        )

    base_qty = int(risk_amount / stop_dist)

    mult_map = {(9, 10): Decimal("1.5"), (7, 8): Decimal("1.0"), (5, 6): Decimal("0.5")}
    # fallback multiplier as Decimal
    mult = Decimal("0.0")
    for (lo, hi), m in mult_map.items():
        if lo <= conviction <= hi:
            mult = m
            break

    final_qty   = int(Decimal(base_qty) * mult)
    capital_dep = Decimal(final_qty) * entry_d

    quant = Decimal("0.01")

    return PositionResult(
        quantity              = final_qty,
        base_quantity         = base_qty,
        conviction_multiplier = float(mult),
        risk_amount           = (risk_amount).quantize(quant, rounding=ROUND_HALF_UP),
        stop_distance         = (stop_dist).quantize(quant, rounding=ROUND_HALF_UP),
        max_loss              = (Decimal(final_qty) * stop_dist).quantize(quant, rounding=ROUND_HALF_UP),
        capital_deployed      = (capital_dep).quantize(quant, rounding=ROUND_HALF_UP),
        capital_pct           = (capital_dep / acct * Decimal("100")).quantize(quant, rounding=ROUND_HALF_UP),
    )


# ═══════════════════════════════════════════════════════════════
#  SL / TARGET CALCULATOR
# ═══════════════════════════════════════════════════════════════

@dataclass
class Levels:
    stop_loss:     Decimal
    stop_distance: Decimal
    target_1:      Decimal
    target_2:      Decimal
    risk_reward:   Decimal
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
    atr_m = Decimal(str(settings.atr_multiplier))
    entry_d = Decimal(str(entry_price))
    atr_d = Decimal(str(atr))

    if direction == "LONG":
        atr_sl    = entry_d - atr_d * atr_m
        swing_sl  = Decimal(str(swing_low)) * Decimal("0.995") if swing_low  else atr_sl
        sl        = max(atr_sl, swing_sl)
        stop_dist = entry_d - sl
        t1 = (entry_d + stop_dist * Decimal(str(target_rr[0]))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        t2 = (entry_d + stop_dist * Decimal(str(target_rr[1]))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        atr_sl    = entry_d + atr_d * atr_m
        swing_sl  = Decimal(str(swing_high)) * Decimal("1.005") if swing_high else atr_sl
        sl        = min(atr_sl, swing_sl)
        stop_dist = sl - entry_d
        t1 = (entry_d - stop_dist * Decimal(str(target_rr[0]))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        t2 = (entry_d - stop_dist * Decimal(str(target_rr[1]))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if stop_dist <= Decimal("0"):
        raise ValueError(f"Computed stop distance is {stop_dist} — bad entry")

    quality = "WIDE_STOP" if stop_dist > atr_d * Decimal(str(settings.max_atr_multiplier)) else "GOOD"

    quant = Decimal("0.01")
    return Levels(
        stop_loss     = (sl).quantize(quant, rounding=ROUND_HALF_UP),
        stop_distance = (stop_dist).quantize(quant, rounding=ROUND_HALF_UP),
        target_1      = t1,
        target_2      = t2,
        risk_reward   = Decimal(str(target_rr[0])).quantize(quant, rounding=ROUND_HALF_UP),
        entry_quality = quality,
    )


# ═══════════════════════════════════════════════════════════════
#  RISK GATES  — run before every order hits DhanHQ
# ═══════════════════════════════════════════════════════════════

@dataclass
class Portfolio:
    account_value: float
    open_positions: int
    daily_pnl_pct: float
    total_exposure_pct: float  # sum of capital_pct across positions


@dataclass
class GateResult:
    passed:  bool
    reason:  str | None = None


async def run_gates(
    trade_decision: TradeDecision,
    portfolio: Portfolio,
) -> GateResult:
    """
    Run all 7 pre-trade risk checks in order. Return first failure.
    All checks are async because they may read Redis.
    """
    # 1. Daily loss limit not breached
    if portfolio.daily_pnl_pct <= -settings.daily_loss_limit_pct:
        return GateResult(False, f"daily_loss_limit: {portfolio.daily_pnl_pct:.2%} <= -{settings.daily_loss_limit_pct:.2%}")

    # 2. Max open positions / exposure not exceeded
    if portfolio.open_positions >= settings.max_open_positions:
        return GateResult(False, f"max_positions: {portfolio.open_positions} >= {settings.max_open_positions}")

    # 3. Conviction score >= minimum threshold
    if trade_decision.conviction < settings.min_conviction:
        return GateResult(False, f"conviction_too_low: {trade_decision.conviction} < {settings.min_conviction}")

    # 4. Reward:Risk ratio >= minimum — require explicit R:R from the model
    if trade_decision.risk_reward is None:
        return GateResult(False, "rr_missing")
    if trade_decision.risk_reward < settings.min_rr:
        return GateResult(False, f"rr_too_low: {trade_decision.risk_reward:.2f} < {settings.min_rr}")

    # 5. Cooldown period passed for the symbol
    if await is_on_cooldown(trade_decision.symbol):
        return GateResult(False, f"cooldown: {trade_decision.symbol} recently traded")

    # 6. Position size fits within per-trade risk %
    position = calculate_position(
        trade_decision.entry_price,
        trade_decision.stop_loss,
        trade_decision.direction,
        trade_decision.conviction,
        portfolio.account_value,
    )
    max_capital_pct = Decimal(str(settings.max_per_trade_risk_pct)) * Decimal("100")
    if position.capital_pct > max_capital_pct:
        return GateResult(False, f"position_size_too_large: {position.capital_pct:.2f}% > {max_capital_pct:.2f}%")

    # 7. No market halt / circuit breaker conditions
    if await is_market_halted():
        return GateResult(False, "market_halt: market is halted")

    return GateResult(True)


async def is_market_halted() -> bool:
    """
    Check NSE circuit-breaker conditions based on cached Nifty 50 change.
    NSE halts trading at -5%, -10%, -20% intraday Nifty drops.
    The scanner writes 'yukti:market:nifty_chg_pct' each cycle.
    """
    from yukti.data.state import get_redis
    try:
        r = await get_redis()
        raw = await r.get("yukti:market:nifty_chg_pct")
        if raw is None:
            return False   # No data yet — don't block
        nifty_chg = float(raw)
        if nifty_chg <= -5.0:
            log.warning("Circuit breaker: Nifty %.2f%% — halting entries", nifty_chg)
            return True
    except Exception as exc:
        log.warning("is_market_halted check failed: %s", exc)
    return False
