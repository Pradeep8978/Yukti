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

    # Capital-based fallback when risk sizing produces zero shares due to
    # account size being too small (risk_amount < stop_distance). This does
    # NOT apply when final_qty is zero because of a low conviction multiplier
    # (conviction < 5 should produce zero qty — a deliberate skip safeguard).
    if final_qty == 0 and base_qty > 0 and mult > Decimal("0"):
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

    # Capital ceiling — risk-based sizing can produce a qty whose margin
    # exceeds the per-stock cap, which on a small account means the broker's
    # RMS rejects the order outright. Cap qty downward so the resulting
    # margin fits within max_single_stock_pct of the account.
    if final_qty > 0:
        margin_per_share = entry_d / lev_d
        if margin_per_share > 0:
            capital_cap = acct * Decimal(str(settings.max_single_stock_pct))
            max_qty_by_capital = int(capital_cap / margin_per_share)
            if final_qty > max_qty_by_capital:
                log.debug(
                    "position sizing: risk-based qty=%d exceeds capital cap "
                    "(margin/share=₹%.2f, cap=₹%.0f); capping qty to %d",
                    final_qty, float(margin_per_share),
                    float(capital_cap), max_qty_by_capital,
                )
                final_qty = max_qty_by_capital

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
#  TRANSACTION COST ESTIMATOR  — NSE intraday equity, DhanHQ retail tariff
# ═══════════════════════════════════════════════════════════════

# Conservative NSE intraday equity cost model. Per leg unless noted:
#   brokerage     : ₹20 flat (or 0.03% — whichever is lower)
#   STT           : 0.025% on sell side only
#   exchange txn  : 0.00322% per leg (NSE Eq, May 2026 ish)
#   SEBI         : ₹10 / crore = 0.0001% per leg (negligible but included)
#   stamp duty   : 0.003% on buy side only
#   GST          : 18% on (brokerage + txn + SEBI) per leg
def estimate_round_trip_cost(notional_per_leg: float) -> float:
    """
    Return estimated INR friction to enter AND exit one intraday equity trade
    of `notional_per_leg` notional. Conservative — meant to over-estimate
    slightly so the cost gate fails closed on borderline setups.

    Components:
      - DhanHQ-style brokerage  (₹20 flat or 0.03% per leg, whichever lower)
      - Exchange transaction charges (0.00322% per leg)
      - SEBI fee (₹10/crore = 0.000001%)
      - GST 18% on (brokerage + txn + SEBI) per leg
      - STT 0.025% (sell side only)
      - Stamp duty 0.003% (buy side only)
      - Slippage assumption: 0.10% per leg = 0.20% round-trip. This is the
        big one for small accounts. Entries use LIMIT (low slippage) but
        EOD exits use MARKET (typical 0.05-0.15% on liquid mid-caps);
        target-hit exits via LIMIT also pay half-spread. 0.10% per leg is
        a realistic average across exit modes.
    """
    if notional_per_leg <= 0:
        return 0.0
    brokerage_per_leg = min(20.0, notional_per_leg * 0.0003)
    txn_per_leg       = notional_per_leg * 0.0000322
    sebi_per_leg      = notional_per_leg * 0.000001
    gst_per_leg       = (brokerage_per_leg + txn_per_leg + sebi_per_leg) * 0.18

    per_leg = brokerage_per_leg + txn_per_leg + sebi_per_leg + gst_per_leg
    round_trip = per_leg * 2

    round_trip += notional_per_leg * 0.00025   # STT (sell side)
    round_trip += notional_per_leg * 0.00003   # stamp duty (buy side)
    round_trip += notional_per_leg * 0.002     # slippage 0.10% × 2 legs
    return round_trip


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
    trade_daily_volume: float | None = None,
) -> GateResult:
    """
    Run up to 10 pre-trade risk checks in order. Return first failure.
    All checks are async because they may read Redis.

    trade_daily_volume: today's total traded volume (shares) for the symbol.
      Used for the liquidity gate — order must be < max_adv_pct of daily volume.
      Pass snap_daily.volume from the scan. None = gate skipped (fail-open).
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

    # 6b. Cost-aware gate — reject signals where expected gross profit at T1
    # doesn't comfortably clear the round-trip transaction cost. On small
    # accounts (sub-₹10k), brokerage + STT + GST routinely eat 100% of the
    # T1 reward on tiny positions, so every "winning" trade still nets a
    # loss. Require gross profit ≥ 1.5× estimated costs.
    t1_target = trade_decision.target_1
    if t1_target is not None and trade_decision.entry_price is not None and position.quantity > 0:
        gross_profit_at_t1 = abs(t1_target - trade_decision.entry_price) * position.quantity
        notional = trade_decision.entry_price * position.quantity
        est_cost = estimate_round_trip_cost(notional)
        # Cost gate disabled when est_cost is zero (paper mode / weird input).
        if est_cost > 0 and gross_profit_at_t1 < est_cost * 1.5:
            return GateResult(
                False,
                f"cost_gate: T1 gross ₹{gross_profit_at_t1:.0f} < 1.5× est. round-trip cost ₹{est_cost:.0f} "
                f"(qty={position.quantity} notional=₹{notional:.0f}) — fees would eat the edge",
            )

    # 7. Single-stock concentration cap — backstop the qty cap inside
    # calculate_position(). On a small account, an unchecked qty produces
    # margin that exceeds broker funds and DhanHQ's RMS rejects with
    # "insufficient funds" — making every signal a no-op. Enforce here too.
    single_stock_cap_pct = Decimal(str(settings.max_single_stock_pct)) * Decimal("100")
    if position.capital_pct > single_stock_cap_pct:
        return GateResult(
            False,
            f"single_stock_cap: margin {position.capital_pct:.2f}% > {single_stock_cap_pct:.2f}% "
            f"(qty={position.quantity}, account=₹{portfolio.account_value:.0f})",
        )

    total_exposure_cap_pct = Decimal(str(settings.max_total_exposure_pct)) * Decimal("100")
    if projected_total_exposure > total_exposure_cap_pct:
        return GateResult(
            False,
            f"total_exposure_cap: {projected_total_exposure:.2f}% > {total_exposure_cap_pct:.2f}%",
        )

    if not ignore_swing_short and trade_decision.holding_period == "swing" and trade_decision.direction == "SHORT":
        return GateResult(False, "swing_short_blocked: NSE equity delivery cannot carry overnight shorts")

    # 7b. Liquidity gate — order must not exceed max_adv_pct of the stock's daily volume.
    # Prevents market-impact on illiquid mid/small-caps where a single order could
    # move the price adversely before the fill completes (NSE 5% circuit risk).
    if trade_daily_volume and trade_daily_volume > 0:
        entry = trade_decision.entry_price or 0.0
        if entry > 0:
            order_notional = float(position.quantity) * entry
            adv_pct = order_notional / (trade_daily_volume * entry)
            if adv_pct > settings.max_adv_pct:
                return GateResult(
                    False,
                    f"liquidity: order is {adv_pct * 100:.1f}% of daily vol "
                    f"({position.quantity} shares, max {settings.max_adv_pct * 100:.0f}%)",
                )

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
    Compute reward:risk from entry / SL / target. Prefers target_2 (the runner
    target the rules engine uses to compute the claimed RR); falls back to
    target_1 when target_2 is unset. Returns None if data is insufficient.
    """
    if td.entry_price is None or td.stop_loss is None:
        return None
    tgt_value = td.target_2 if td.target_2 is not None else td.target_1
    if tgt_value is None:
        return None
    entry = Decimal(str(td.entry_price))
    sl = Decimal(str(td.stop_loss))
    tgt = Decimal(str(tgt_value))
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
