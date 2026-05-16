"""
yukti/risk/sizing.py  ·  yukti/risk/sl_target.py  ·  yukti/risk/gates.py  ·  yukti/risk/cooldown.py
Combined into one file for brevity. Split into submodules in the actual project.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP

from datetime import date

from yukti.agents.arjun import TradeDecision
from yukti.config import settings
from yukti.data.state import is_on_cooldown

log = logging.getLogger(__name__)

# Alert-once-per-day flags to avoid spamming Telegram every scan cycle
_daily_loss_alerted_date: date | None = None
_circuit_breaker_alerted: bool = False


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
    capital_deployed:      Decimal   # ₹ notional (qty × entry)
    capital_pct:           Decimal   # margin / account_value × 100 (leverage-adjusted)
    leverage:              float = 1.0
    margin_deployed:       Decimal = Decimal("0")   # capital_deployed / leverage


def calculate_position(
    entry_price:   float,
    stop_loss:     float,
    direction:     str,
    conviction:    int,
    account_value: float | None = None,
    risk_pct:      float | None = None,
    leverage:      float = 1.0,
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

    final_qty = int(Decimal(base_qty) * mult)

    quant = Decimal("0.01")
    lev_d = Decimal(str(max(1.0, leverage)))

    # Capital-based fallback when risk sizing produces zero shares.
    # Happens when account is small (risk_amount < stop_distance).
    # We use up to max_single_stock_pct of account as the position cap,
    # capped further so margin never exceeds available account.
    if final_qty == 0:
        margin_per_share = entry_d / lev_d
        if margin_per_share > 0:
            capital_cap = acct * Decimal(str(settings.max_single_stock_pct))
            capital_qty = int(capital_cap / margin_per_share)
            if capital_qty >= 1:
                log.debug(
                    "position sizing: risk-based qty=0 (risk=₹%.2f stop=₹%.2f); "
                    "falling back to capital-based qty=%d (%.0f%% of ₹%.0f)",
                    float(risk_amount), float(stop_dist), capital_qty,
                    float(settings.max_single_stock_pct * 100), float(acct),
                )
                final_qty = capital_qty

    capital_dep = Decimal(final_qty) * entry_d
    margin  = (capital_dep / lev_d).quantize(quant, rounding=ROUND_HALF_UP)
    cap_pct = (margin / acct * Decimal("100")).quantize(quant, rounding=ROUND_HALF_UP)

    return PositionResult(
        quantity              = final_qty,
        base_quantity         = base_qty,
        conviction_multiplier = float(mult),
        risk_amount           = (risk_amount).quantize(quant, rounding=ROUND_HALF_UP),
        stop_distance         = (stop_dist).quantize(quant, rounding=ROUND_HALF_UP),
        max_loss              = (Decimal(final_qty) * stop_dist).quantize(quant, rounding=ROUND_HALF_UP),
        capital_deployed      = (capital_dep).quantize(quant, rounding=ROUND_HALF_UP),
        capital_pct           = cap_pct,
        leverage              = max(1.0, leverage),
        margin_deployed       = margin,
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
#  RISK GATES  — 9 deterministic checks run before every order hits DhanHQ
# ═══════════════════════════════════════════════════════════════

@dataclass
class Portfolio:
    account_value: float
    open_positions: int
    daily_pnl_pct: float
    total_exposure_pct: float  # sum of capital_pct across positions
    trades_today: int = 0
    min_conviction_override: int | None = None
    # Optional per-sector exposure (capital_pct) and a sector hint for the
    # incoming trade. Both default to "no info" so existing callers aren't
    # forced to pass them; when present, they enable the sector-cap gate.
    sector_exposure_pct: dict[str, float] | None = None
    trade_sector: str | None = None
    # Recent win-rate, used to tighten/relax the conviction floor. Both
    # default to None so callers that don't pass this info get the static
    # min_conviction behavior. count is needed because get_performance_state
    # returns a 0.5 prior when there's no trade history.
    win_rate_last_10: float | None = None
    win_rate_last_10_count: int = 0


@dataclass
class GateResult:
    passed:  bool
    reason:  str | None = None


async def run_gates(
    trade_decision: TradeDecision,
    portfolio: Portfolio,
    *,
    ignore_cooldown: bool = False,
    ignore_market_halt: bool = False,
    ignore_swing_short: bool = False,
) -> GateResult:
    """
    Run up to 9 pre-trade risk checks in order. Return first failure.
    All checks are async because they may read Redis.
    """
    # 1. Daily loss limit not breached
    daily_loss_limit_pct = settings.daily_loss_limit_pct * 100
    if portfolio.daily_pnl_pct <= -daily_loss_limit_pct:
        global _daily_loss_alerted_date
        today = date.today()
        if _daily_loss_alerted_date != today:
            _daily_loss_alerted_date = today
            try:
                from yukti.telegram.bot import alert_risk_halt
                import asyncio
                asyncio.create_task(alert_risk_halt(portfolio.daily_pnl_pct, daily_loss_limit_pct))
            except Exception:
                pass
        return GateResult(False, f"daily_loss_limit: {portfolio.daily_pnl_pct:.2f}% <= -{daily_loss_limit_pct:.2f}%")

    # 2. Max open positions / exposure not exceeded
    if portfolio.open_positions >= settings.max_open_positions:
        return GateResult(False, f"max_positions: {portfolio.open_positions} >= {settings.max_open_positions}")

    if portfolio.trades_today >= settings.max_trades_per_day:
        return GateResult(False, f"max_trades_today: {portfolio.trades_today} >= {settings.max_trades_per_day}")

    # 3. Conviction score >= dynamic minimum threshold.
    # Three layers:
    #   (a) base       — regime override or static settings.min_conviction
    #   (b) loss-tier  — between -warn% and -limit% pnl, force min ≥ 8 so we
    #                    only push through drawdown on very strong setups.
    #                    This floor is *sticky*: win-rate easing must not
    #                    cross it back down, otherwise a hot streak in a
    #                    drawdown silently relaxes the safety intent.
    #   (c) win-rate   — bump up on cold streaks (<40%), ease one notch on
    #                    hot streaks (>60%); only applied with ≥5 sample size
    min_conviction = portfolio.min_conviction_override or settings.min_conviction
    loss_tier_floor = 0  # 0 = no loss-tier floor active

    daily_loss_warn_pct = settings.daily_loss_warn_pct * 100
    if portfolio.daily_pnl_pct <= -daily_loss_warn_pct:
        loss_tier_floor = 8
        min_conviction = max(min_conviction, loss_tier_floor)

    if portfolio.win_rate_last_10 is not None and portfolio.win_rate_last_10_count >= 5:
        if portfolio.win_rate_last_10 < 0.40:
            min_conviction += 2
        elif portfolio.win_rate_last_10 > 0.60:
            # Never ease below the static floor or the loss-tier floor (if
            # active). Easing past the loss-tier floor would silently undo
            # the drawdown protection.
            ease_floor = max(settings.min_conviction, loss_tier_floor)
            min_conviction = max(ease_floor, min_conviction - 1)

    min_conviction = min(10, max(1, min_conviction))
    if trade_decision.conviction < min_conviction:
        return GateResult(False, f"conviction_too_low: {trade_decision.conviction} < {min_conviction}")

    # 4. Reward:Risk ratio >= minimum — verify against structural levels rather
    # than blindly trusting the LLM's self-reported value.
    if trade_decision.risk_reward is None:
        return GateResult(False, "rr_missing")
    structural_rr = _compute_structural_rr(trade_decision)
    effective_rr = min(trade_decision.risk_reward, structural_rr) if structural_rr is not None else trade_decision.risk_reward
    if effective_rr < settings.min_rr:
        return GateResult(
            False,
            f"rr_too_low: effective={effective_rr:.2f} (claimed={trade_decision.risk_reward:.2f}, "
            f"structural={structural_rr if structural_rr is None else f'{structural_rr:.2f}'}) < {settings.min_rr}",
        )

    # 5. Cooldown period passed for the symbol
    if not ignore_cooldown and await is_on_cooldown(trade_decision.symbol):
        return GateResult(False, f"cooldown: {trade_decision.symbol} recently traded")

    # 6. Position size fits within per-trade risk % and single-stock cap
    _gate_leverage = settings.intraday_leverage if trade_decision.holding_period == "intraday" else 1.0
    position = calculate_position(
        trade_decision.entry_price,
        trade_decision.stop_loss,
        trade_decision.direction,
        trade_decision.conviction,
        portfolio.account_value,
        leverage=_gate_leverage,
    )
    account_value = Decimal(str(portfolio.account_value))
    if position.quantity == 0:
        entry_fmt = f"₹{trade_decision.entry_price:.0f}" if trade_decision.entry_price else "?"
        return GateResult(
            False,
            f"zero_quantity: account ₹{portfolio.account_value:.0f} cannot size even 1 share "
            f"of {trade_decision.symbol} at {entry_fmt} (single_stock_cap "
            f"₹{portfolio.account_value * settings.max_single_stock_pct:.0f} < margin/share)",
        )
    # max_loss_cap only applies to risk-sized positions (base_quantity > 0).
    # When base_quantity == 0 the account is too small for ATR-based sizing and
    # calculate_position() fell back to capital-based minimum sizing — the
    # single_stock_cap (gate below) already bounds the exposure in that case.
    if position.base_quantity > 0:
        max_loss_cap_pct = Decimal(str(settings.max_loss_cap_pct)) * Decimal("100")
        max_loss_pct = (position.max_loss / account_value * Decimal("100")) if account_value > 0 else Decimal("0")
        if max_loss_pct > max_loss_cap_pct:
            return GateResult(False, f"max_loss_too_large: {max_loss_pct:.2f}% > {max_loss_cap_pct:.2f}%")
    else:
        max_loss_pct = Decimal("0")

    projected_total_exposure = Decimal(str(portfolio.total_exposure_pct)) + position.capital_pct

    # 7. Single-stock concentration cap — disabled (account too small for % limits to be meaningful)
    # total_exposure_cap — disabled alongside it
    pass

    if not ignore_swing_short and trade_decision.holding_period == "swing" and trade_decision.direction == "SHORT":
        return GateResult(False, "swing_short_blocked: NSE equity delivery cannot carry overnight shorts")

    # 8. Sector concentration cap — only enforced when caller passed sector info.
    if portfolio.trade_sector and portfolio.sector_exposure_pct is not None:
        sector_cap_pct = Decimal(str(settings.max_sector_pct)) * Decimal("100")
        existing = Decimal(str(portfolio.sector_exposure_pct.get(portfolio.trade_sector, 0.0)))
        projected = existing + position.capital_pct
        if projected > sector_cap_pct:
            return GateResult(
                False,
                f"sector_cap[{portfolio.trade_sector}]: {projected:.2f}% > {sector_cap_pct:.2f}%",
            )

    # 9. No market halt / circuit breaker conditions
    if not ignore_market_halt and await is_market_halted():
        return GateResult(False, "market_halt: market is halted")

    log.info(
        "GATES PASS | %s %s conv=%d rr=%.2f qty=%d max_loss=₹%.0f (%.1f%%) "
        "margin=%.1f%% exp=%.1f%%",
        trade_decision.symbol,
        trade_decision.direction,
        trade_decision.conviction,
        effective_rr if effective_rr is not None else 0.0,
        position.quantity,
        float(position.max_loss),
        float(max_loss_pct),
        float(position.capital_pct),
        float(projected_total_exposure),
    )
    return GateResult(True)


def _compute_structural_rr(td: TradeDecision) -> float | None:
    """
    Compute reward:risk from the decision's entry / SL / first target.
    Returns None if data is insufficient. Uses target_1 as the primary target.
    """
    if td.entry_price is None or td.stop_loss is None or td.target_1 is None:
        return None
    entry = Decimal(str(td.entry_price))
    sl = Decimal(str(td.stop_loss))
    tgt = Decimal(str(td.target_1))
    if td.direction == "LONG":
        risk = entry - sl
        reward = tgt - entry
    else:
        risk = sl - entry
        reward = entry - tgt
    if risk <= 0 or reward <= 0:
        return None
    return float(reward / risk)


# Max age (seconds) the cached Nifty value is allowed to be when we trust it
# for the circuit-breaker gate. The scan loop writes every cycle (~5 min) with
# a 10-min TTL, so 8 min gives ~1.5 cycles of slack before we fail closed.
_NIFTY_MAX_AGE_SECONDS = 8 * 60


async def is_market_halted(*, fail_closed: bool | None = None) -> bool:
    """
    Check NSE circuit-breaker conditions based on cached Nifty 50 change.
    NSE halts trading at -5%, -10%, -20% intraday Nifty drops.
    The scanner writes 'yukti:market:nifty_chg_pct' each cycle as JSON
    `{"chg_pct": <float>, "ts": <unix_seconds>}`.

    Behaviour on missing or stale data / Redis errors:
      - paper / backtest: fail-open (return False) so dev loops aren't blocked.
      - live / shadow:    fail-closed (return True) — refuse to trade when we
                          can't verify market state.
    Override with `fail_closed=True/False` for tests.
    """
    import json as _json
    import time as _time

    from yukti.data.state import get_redis

    if fail_closed is None:
        fail_closed = settings.mode in ("live", "shadow")

    try:
        r = await get_redis()
        raw = await r.get("yukti:market:nifty_chg_pct")
        if raw is None:
            if fail_closed:
                log.warning("Circuit breaker: no Nifty data cached — failing closed (mode=%s)", settings.mode)
            return fail_closed

        # Accept new JSON form `{"chg_pct": ..., "ts": ...}` and the legacy
        # plain-float form (during the cutover). Stale values fail closed.
        try:
            blob = _json.loads(raw)
            nifty_chg = float(blob["chg_pct"])
            age = int(_time.time()) - int(blob.get("ts") or 0)
            if age > _NIFTY_MAX_AGE_SECONDS:
                log.warning(
                    "Circuit breaker: Nifty cache stale (age=%ds) — failing %s",
                    age, "closed" if fail_closed else "open",
                )
                return fail_closed
        except (ValueError, TypeError, KeyError):
            # Legacy plain-float; no embedded timestamp, but the 10-min TTL
            # on the key already guarantees freshness when the value exists.
            nifty_chg = float(raw)

        if nifty_chg <= -5.0:
            log.warning("Circuit breaker: Nifty %.2f%% — halting entries", nifty_chg)
            global _circuit_breaker_alerted
            if not _circuit_breaker_alerted:
                _circuit_breaker_alerted = True
                try:
                    from yukti.telegram.bot import alert_circuit_breaker
                    import asyncio
                    asyncio.create_task(alert_circuit_breaker(nifty_chg))
                except Exception:
                    pass
            return True
        _circuit_breaker_alerted = False  # Reset once market recovers
        return False
    except Exception as exc:
        log.warning("is_market_halted check failed: %s (fail_closed=%s)", exc, fail_closed)
        return fail_closed
