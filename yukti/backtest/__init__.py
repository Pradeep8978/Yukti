"""
yukti/backtest/paper_broker.py   — simulated DhanHQ for paper trading and backtest
yukti/backtest/engine.py         — historical candle replay through the full agent
yukti/backtest/report.py         — equity curve, metrics, and performance stats
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from yukti.config import settings

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  PAPER BROKER
#  Drop-in replacement for DhanClient in paper / backtest mode
# ═══════════════════════════════════════════════════════════════

@dataclass
class SimPosition:
    symbol:       str
    direction:    str
    quantity:     int
    entry_price:  float
    stop_loss:    float
    target_1:     float
    target_2:     float | None
    holding:      str
    entry_time:   datetime
    status:       str = "OPEN"
    exit_price:   float = 0.0
    exit_reason:  str  = ""
    pnl:          float = 0.0
    pnl_pct:      float = 0.0
    exit_time:    datetime | None = None
    # Partial-exit / breakeven-trail state.
    # original_quantity records the size at entry so pnl_pct can be normalized
    # against full exposure even after a T1 partial.
    # partial_pnl is the realized PnL from the T1 partial; total pnl =
    # partial_pnl + runner_pnl_at_close. partial_exit_price > 0 signals
    # "phase 2" (stop is now at breakeven, target is T2).
    original_quantity:   int   = 0
    partial_exit_price:  float = 0.0
    partial_exit_qty:    int   = 0
    partial_pnl:         float = 0.0


class PaperBroker:
    """
    Simulates DhanHQ order execution for paper trading and backtesting.
    Trades are filled at the next available price (limit: at limit or better).
    GTT orders trigger when price crosses the trigger level.
    """

    def __init__(self, account_value: float = 500_000.0, slippage_pct: float = 0.001) -> None:
        self.initial_account_value = account_value
        self.account_value  = account_value
        self.slippage_pct   = slippage_pct          # 0.1% default slippage
        self.positions:     dict[str, SimPosition] = {}
        self.closed_trades: list[SimPosition]      = []
        self.order_counter  = 0
        self._current_prices: dict[str, float] = {}

    def update_prices(
        self,
        current_time: datetime,
        prices: dict[str, float],
        highs:  dict[str, float] | None = None,
        lows:   dict[str, float] | None = None,
    ) -> None:
        """Called each candle. highs/lows enable accurate daily-candle GTT checks."""
        self._current_prices.update(prices)
        self._check_gtts(current_time, highs or {}, lows or {})

    def _check_gtts(
        self,
        current_time: datetime,
        highs: dict[str, float],
        lows:  dict[str, float],
    ) -> None:
        """Check if any position's SL or target has been hit.
        Uses intraday high/low when available (daily candles); falls back to close.
        On a same-bar SL+target hit we conservatively assume SL fired first."""
        for symbol, pos in list(self.positions.items()):
            if pos.status != "OPEN":
                continue
            close = self._current_prices.get(symbol)
            if not close:
                continue
            high = highs.get(symbol, close)
            low  = lows.get(symbol, close)

            if pos.direction == "LONG":
                if low <= pos.stop_loss:
                    self._close_position(symbol, pos.stop_loss * (1 - self.slippage_pct), "stop_loss_hit", current_time)
                elif high >= pos.target_1:
                    self._close_position(symbol, pos.target_1, "target_1_hit", current_time)
            else:  # SHORT
                if high >= pos.stop_loss:
                    self._close_position(symbol, pos.stop_loss * (1 + self.slippage_pct), "stop_loss_hit", current_time)
                elif low <= pos.target_1:
                    self._close_position(symbol, pos.target_1, "target_1_hit", current_time)

    def _close_position(self, symbol: str, exit_price: float, reason: str, exit_time: datetime | None = None) -> None:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return
        is_long = pos.direction == "LONG"
        runner_pnl = (exit_price - pos.entry_price) * pos.quantity if is_long \
            else (pos.entry_price - exit_price) * pos.quantity
        pos.exit_price  = exit_price
        pos.exit_reason = reason
        pos.pnl         = runner_pnl + pos.partial_pnl
        # pnl_pct is computed against the *original* exposure so partial-exit
        # winners show a comparable % regardless of how the position was scaled.
        denom_qty = pos.original_quantity if pos.original_quantity > 0 else pos.quantity
        pos.pnl_pct     = pos.pnl / (pos.entry_price * max(denom_qty, 1)) * 100
        pos.exit_time   = exit_time
        pos.status      = "CLOSED"
        self.account_value += runner_pnl
        self.closed_trades.append(pos)
        log.debug("Paper closed: %s %s P&L=%.1f%%", symbol, reason, pos.pnl_pct)

    # ── API-compatible methods (mirror DhanClient interface) ──────────────────

    async def place_order(
        self,
        security_id: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        product_type: str,
        price: float = 0.0,
        trigger_price: float = 0.0,
        tag: str = "",
    ) -> dict[str, Any]:
        self.order_counter += 1
        # Simulate fill at price + slippage
        fill_price = price if price > 0 else self._current_prices.get(security_id, price)
        if transaction_type == "BUY":
            fill_price *= (1 + self.slippage_pct)
        else:
            fill_price *= (1 - self.slippage_pct)

        return {
            "orderId": f"SIM-{self.order_counter:06d}",
            "status":  "TRADED",
            "filledQty": quantity,
            "averagePrice": fill_price,
        }

    async def place_gtt(self, **kwargs: Any) -> dict[str, Any]:
        self.order_counter += 1
        return {"gttOrderId": f"GTT-{self.order_counter:06d}"}

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"status": "CANCELLED"}

    async def cancel_gtt(self, gtt_id: str) -> dict[str, Any]:
        return {"status": "CANCELLED"}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        return {"orderStatus": "TRADED", "filledQty": 100, "averagePrice": 0.0}

    async def get_positions(self) -> list[dict[str, Any]]:
        return [
            {"tradingSymbol": sym, "netQty": pos.quantity}
            for sym, pos in self.positions.items()
        ]

    async def market_exit(self, security_id: str, direction: str, quantity: int, product_type: str) -> dict[str, Any]:
        price = self._current_prices.get(security_id, 0.0)
        if price <= 0:
            pos = self.positions.get(security_id)
            if pos is not None:
                log.warning(
                    "market_exit %s: no current price — using entry ₹%.2f (PnL=0)",
                    security_id, pos.entry_price,
                )
                price = pos.entry_price
        self._close_position(security_id, price, "eod_squareoff")
        return {"orderId": f"EXIT-{self.order_counter}", "status": "TRADED"}


# ═══════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
#  Replays historical candles through the full Yukti agent
# ═══════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Replays historical OHLCV candles symbol-by-symbol, cycle-by-cycle.
    On each candle close, runs the full Yukti signal pipeline and either
    the AI agent (Arjun/Gemini/Claude) or the deterministic rules engine.
    Uses PaperBroker for simulated fills.

    use_rules_engine=True  → calls rules_engine.decide() (zero API cost, zero latency)
    use_rules_engine=False → calls arjun.safe_decide() (AI path, respects claude_sample_rate)
    """

    def __init__(
        self,
        candles:           dict[str, pd.DataFrame],  # symbol → OHLCV df
        nifty_candles:     pd.DataFrame,
        account_value:     float = 500_000.0,
        claude_sample_rate: float = 1.0,   # fraction of candles sent to AI (reduce to cut cost)
        use_rules_engine:  bool  = False,  # True → deterministic rules, no API calls
        decision_start:    date | None = None,  # only open new trades on/after this date
    ) -> None:
        self.candles             = candles
        self.nifty_candles       = nifty_candles
        self.broker              = PaperBroker(account_value)
        self.claude_sample_rate  = claude_sample_rate
        self.use_rules_engine    = use_rules_engine
        self.decision_start      = decision_start
        self._equity_curve:      list[dict] = []
        self._cooldowns:         dict[str, datetime] = {}
        self._closed_trades_seen = 0

    def _trades_today(self, ts: datetime) -> int:
        trade_day = ts.date()
        closed_today = sum(1 for trade in self.broker.closed_trades if trade.exit_time and trade.exit_time.date() == trade_day)
        open_today = sum(1 for trade in self.broker.positions.values() if trade.entry_time.date() == trade_day)
        return closed_today + open_today

    def _daily_pnl_pct(self, ts: datetime) -> float:
        trade_day = ts.date()
        return float(sum(trade.pnl_pct for trade in self.broker.closed_trades if trade.exit_time and trade.exit_time.date() == trade_day))

    def _win_rate_last_10(self) -> tuple[float | None, int]:
        recent = self.broker.closed_trades[-10:]
        if not recent:
            return None, 0
        wins = sum(1 for trade in recent if trade.pnl > 0)
        return wins / len(recent), len(recent)

    def _consecutive_losses(self) -> int:
        streak = 0
        for trade in reversed(self.broker.closed_trades):
            if trade.pnl >= 0:
                break
            streak += 1
        return streak

    def _total_exposure_pct(self) -> float:
        account_value = max(self.broker.account_value, 1.0)
        total_margin = 0.0
        for trade in self.broker.positions.values():
            leverage = settings.intraday_leverage if trade.holding == "intraday" else 1.0
            total_margin += (trade.entry_price * trade.quantity) / max(leverage, 1.0)
        return round(total_margin / account_value * 100, 2) if account_value else 0.0

    def _cooldown_until(self, ts: datetime, conviction: int, cycle_delta: timedelta) -> datetime:
        cycles = settings.cooldown_cycles
        if conviction >= 8:
            cycles = max(1, cycles // 2)
        return ts + (cycle_delta * cycles)

    def _register_new_cooldowns(self, ts: datetime, cycle_delta: timedelta) -> None:
        while self._closed_trades_seen < len(self.broker.closed_trades):
            trade = self.broker.closed_trades[self._closed_trades_seen]
            exit_ts = trade.exit_time or ts
            self._cooldowns[trade.symbol] = self._cooldown_until(exit_ts, trade.conviction if hasattr(trade, "conviction") else 0, cycle_delta)
            self._closed_trades_seen += 1

    def _is_on_cooldown(self, symbol: str, ts: datetime) -> bool:
        until = self._cooldowns.get(symbol)
        if until is None:
            return False
        if ts >= until:
            self._cooldowns.pop(symbol, None)
            return False
        return True

    def _squareoff(self, sym: str, prices: dict[str, float], ts: datetime) -> None:
        """End-of-period forced exit. Pick the best available price:
        1) this bar's close if the symbol has a candle here,
        2) the broker's last-known close (forward-filled across prior bars),
        3) the position's entry price as a final safety fallback (PnL=0).
        Exiting at 0.0 — which is what a naive prices.get(sym, 0.0) would do —
        registers a fake -100% loss whenever a position's symbol has a data
        gap on the squareoff bar (holiday, halt, late-listed peer, etc.)."""
        pos = self.broker.positions.get(sym)
        if pos is None:
            return
        exit_price = prices.get(sym)
        if exit_price is None or exit_price <= 0:
            exit_price = self.broker._current_prices.get(sym)
        if exit_price is None or exit_price <= 0:
            log.warning(
                "Squareoff %s @ %s: no price available — using entry price ₹%.2f (PnL=0)",
                sym, ts, pos.entry_price,
            )
            exit_price = pos.entry_price
        self.broker._close_position(sym, exit_price, "eod_squareoff", ts)

    async def run(self) -> "BacktestReport":
        """Run the full backtest and return a report."""
        from yukti.signals.indicators import compute_full, snapshot_at
        from yukti.risk import Portfolio, calculate_position, calculate_levels, run_gates

        if self.use_rules_engine:
            from yukti.agents.rules_engine import decide as rules_decide
            from yukti.services.macro_context_service import MacroContext, fetch_macro_context
            from yukti.signals.patterns import (
                breakout, breakdown,
                trend_pullback_long, trend_pullback_short,
                reversal_long, reversal_short,
                momentum_long, momentum_short,
                orb_breakout, vwap_bounce,
            )

            def _detect_pattern(snap, candles_today=None, current_time=None, snap_daily=None):
                # Mirror the live scanner's coverage: include all 10 patterns,
                # including momentum_short (which was previously missing and
                # tilted the backtest LONG-heavy), and the intraday ORB / VWAP
                # bounce patterns when we have intraday candles + a clock.
                candidates = [
                    breakout(snap), breakdown(snap),
                    trend_pullback_long(snap), trend_pullback_short(snap),
                    reversal_long(snap), reversal_short(snap),
                    momentum_long(snap), momentum_short(snap),
                ]
                if candles_today is not None and current_time is not None:
                    candidates.append(orb_breakout(snap, candles_today, current_time, snap_daily))
                    candidates.append(vwap_bounce(snap, candles_today, current_time, snap_daily))
                detected = [p for p in candidates if p.detected]
                return max(detected, key=lambda p: p.strength) if detected else None
        else:
            from yukti.agents.arjun import arjun
            from yukti.signals.context import build_context

        symbols    = list(self.candles.keys())
        all_dates  = sorted(set(
            idx for df in self.candles.values() for idx in df.index
        ))

        # Detect timeframe: daily candles have no intraday time component
        is_daily = all_dates and (
            hasattr(all_dates[0], "time") and all_dates[0].time().hour == 0
            and all_dates[0].time().minute == 0
        )
        tf_label = "daily" if is_daily else "intraday"
        cycle_delta = timedelta(days=1) if is_daily else timedelta(minutes=5)
        if len(all_dates) >= 2:
            observed_delta = all_dates[1] - all_dates[0]
            if observed_delta.total_seconds() > 0:
                cycle_delta = observed_delta
        log.info(
            "Backtest: %d symbols × %d candles  mode=%s  timeframe=%s",
            len(symbols), len(all_dates),
            "rules_engine" if self.use_rules_engine else "ai",
            tf_label,
        )

        # Precompute indicators once per symbol on the full series.
        # Avoids the O(n²) cost of running pandas_ta.supertrend (Python loop) on
        # a growing slice for every (ts, symbol). Daily backtest with 220-day
        # warmup × 50 symbols was ~3 min before this; now well under 10 sec.
        full_indicators: dict[str, pd.DataFrame] = {}
        tf_for_indicators = "daily" if is_daily else "5m"
        for sym, df in self.candles.items():
            if len(df) < 60:
                continue
            try:
                full_indicators[sym] = compute_full(df, timeframe=tf_for_indicators)
            except Exception as exc:
                log.debug("precompute failed for %s: %s", sym, exc)

        # Additionally precompute daily indicators for multi-timeframe alignment
        # when running intraday backtests. This enables rules_engine to consult
        # a true daily snapshot (rsi, adx, daily S/R) and for patterns that
        # depend on daily alignment.
        full_indicators_daily: dict[str, pd.DataFrame] = {}
        if not is_daily:
            for sym, df in self.candles.items():
                try:
                    # Resample to calendar-day bars using volume-weighted aggregation
                    daily = df.resample('D').agg({
                        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
                    }).dropna()
                    if len(daily) < 60:
                        continue
                    full_indicators_daily[sym] = compute_full(daily, timeframe='daily')
                except Exception:
                    # best-effort: missing daily series degrades gracefully
                    continue

        for ts in all_dates:
            nifty_slice = self.nifty_candles.loc[:ts]
            if len(nifty_slice) < 50:
                continue

            nifty_chg   = float((nifty_slice["close"].iloc[-1] - nifty_slice["close"].iloc[-2]) / nifty_slice["close"].iloc[-2] * 100)
            nifty_trend = "UP" if nifty_slice["close"].iloc[-1] > nifty_slice["close"].iloc[-10] else "DOWN"

            # Pass high/low to broker so GTT checks are accurate on daily candles
            prices = {}
            highs  = {}
            lows   = {}
            for sym, df in self.candles.items():
                if ts in df.index:
                    prices[sym] = float(df.loc[ts, "close"])
                    highs[sym]  = float(df.loc[ts, "high"])
                    lows[sym]   = float(df.loc[ts, "low"])
            self.broker.update_prices(ts, prices, highs, lows)

            # Daily candles: square off at end of each week (Friday)
            if is_daily and hasattr(ts, "dayofweek") and ts.dayofweek == 4:
                for sym in list(self.broker.positions.keys()):
                    self._squareoff(sym, prices, ts)
            # Intraday: square off at 15:15 IST
            elif not is_daily and hasattr(ts, "time") and ts.time().hour == 15 and ts.time().minute >= 15:
                for sym in list(self.broker.positions.keys()):
                    self._squareoff(sym, prices, ts)

            self._register_new_cooldowns(ts, cycle_delta)

            # Warmup pre-roll: feed indicator history but don't open new trades.
            ts_date = ts.date() if hasattr(ts, "date") else None
            if self.decision_start is not None and ts_date is not None and ts_date < self.decision_start:
                continue

            # Cutoff: block new intraday entries inside the last 20 min before
            # squareoff (matches the live scanner's _entry_cutoff). Without
            # this, a trade opened at 14:56 has no room to reach a 2R target.
            if not is_daily and hasattr(ts, "time"):
                t = ts.time()
                if t.hour > 14 or (t.hour == 14 and t.minute >= 55):
                    continue

            current_time = ts.time() if (not is_daily and hasattr(ts, "time")) else None

            for symbol in symbols:
                if symbol in self.broker.positions:
                    continue
                if self._is_on_cooldown(symbol, ts):
                    continue
                df = self.candles[symbol].loc[:ts]
                if len(df) < 60:
                    continue

                # AI path only: sample rate gate to limit API spend
                if not self.use_rules_engine:
                    import random
                    if random.random() > self.claude_sample_rate:
                        continue

                full_df = full_indicators.get(symbol)
                if full_df is None:
                    continue
                try:
                    snap = snapshot_at(full_df, ts, timeframe=tf_for_indicators)
                except Exception:
                    continue

                perf = {
                    "consecutive_losses": self._consecutive_losses(),
                    "daily_pnl_pct":      self._daily_pnl_pct(ts),
                    "win_rate_last_10":   self._win_rate_last_10()[0] if self._win_rate_last_10()[0] is not None else 0.5,
                    "win_rate_last_10_count": self._win_rate_last_10()[1],
                    "trades_today":       self._trades_today(ts),
                }

                # ── Decision ──────────────────────────────────────────
                if self.use_rules_engine:
                    # Build a rich MacroContext using the same fetcher the live
                    # scanner uses. This loads VIX, PCR, option metrics, and
                    # headlines (best-effort, cached) so the rules engine has full
                    # multi-source signals available during backtests.
                    try:
                        macro = await fetch_macro_context(nifty_chg, nifty_trend)
                    except Exception:
                        # Fallback to minimal context if the async fetch fails
                        macro = MacroContext(nifty_chg_pct=nifty_chg, nifty_trend=nifty_trend)

                    # Same-day candle slice enables ORB / VWAP-bounce detection.
                    candles_today = None
                    if not is_daily and ts_date is not None:
                        try:
                            sym_df = self.candles[symbol]
                            mask = sym_df.index.normalize() == pd.Timestamp(ts_date)
                            today_slice = sym_df.loc[mask]
                            if len(today_slice) >= 3:
                                candles_today = today_slice
                        except Exception:
                            candles_today = None

                    # Build a daily snapshot (snap_daily) for multi-timeframe checks
                    snap_daily = None
                    try:
                        if is_daily:
                            daily_full = full_indicators.get(symbol)
                            if daily_full is not None:
                                snap_daily = snapshot_at(daily_full, ts, timeframe='daily')
                        else:
                            daily_full = full_indicators_daily.get(symbol) if 'full_indicators_daily' in locals() else None
                            if daily_full is not None and ts_date is not None:
                                snap_daily = snapshot_at(daily_full, pd.Timestamp(ts_date), timeframe='daily')
                    except Exception:
                        snap_daily = None

                    pattern = _detect_pattern(snap, candles_today, current_time, snap_daily)
                    decision, _ = rules_decide(symbol, snap, macro, perf, pattern, snap_daily)
                else:
                    context = build_context(
                        symbol, snap,
                        nifty_change_pct=nifty_chg,
                        nifty_trend=nifty_trend,
                        news_summary="backtest mode — no live news",
                        perf=perf,
                    )
                    decision = await arjun.safe_decide(context)

                if decision.action == "SKIP":
                    continue

                if self.broker.account_value <= 0:
                    continue

                # Fill levels from AI decision or compute fallback
                if not decision.stop_loss or not decision.target_1:
                    levels = calculate_levels(
                        decision.direction or "LONG",
                        decision.entry_price or snap.close,
                        snap.atr,
                        snap.nearest_swing_low,
                        snap.nearest_swing_high,
                    )
                    decision.stop_loss   = decision.stop_loss  or levels.stop_loss
                    decision.target_1    = decision.target_1   or levels.target_1
                    decision.target_2    = decision.target_2   or levels.target_2
                    decision.risk_reward = decision.risk_reward or levels.risk_reward

                portfolio = Portfolio(
                    account_value=max(self.broker.account_value, 1.0),
                    open_positions=len(self.broker.positions),
                    daily_pnl_pct=perf["daily_pnl_pct"],
                    total_exposure_pct=self._total_exposure_pct(),
                    trades_today=perf["trades_today"],
                    win_rate_last_10=perf["win_rate_last_10"],
                    win_rate_last_10_count=perf["win_rate_last_10_count"],
                )
                gate = await run_gates(
                    decision,
                    portfolio,
                    ignore_cooldown=True,
                    ignore_market_halt=True,
                )
                if not gate.passed:
                    continue

                try:
                    position = calculate_position(
                        entry_price = decision.entry_price or snap.close,
                        stop_loss   = decision.stop_loss,
                        direction   = decision.direction or "LONG",
                        conviction  = decision.conviction,
                        account_value = max(self.broker.account_value, 1.0),
                        leverage = settings.intraday_leverage if decision.holding_period == "intraday" else 1.0,
                    )
                except ValueError:
                    continue

                if decision.risk_reward < settings.min_rr or position.quantity == 0:
                    continue

                # Simulate fill
                fill_resp = await self.broker.place_order(
                    security_id      = symbol,
                    transaction_type = "BUY" if decision.direction == "LONG" else "SELL",
                    quantity         = position.quantity,
                    order_type       = "LIMIT",
                    product_type     = "INTRADAY",
                    price            = decision.entry_price or snap.close,
                )

                fill_price = float(fill_resp.get("averagePrice", snap.close))

                sim_pos = SimPosition(
                    symbol       = symbol,
                    direction    = decision.direction or "LONG",
                    quantity     = position.quantity,
                    entry_price  = fill_price,
                    stop_loss    = decision.stop_loss,
                    target_1     = decision.target_1,
                    target_2     = decision.target_2,
                    holding      = decision.holding_period,
                    entry_time   = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts)),
                    original_quantity = position.quantity,
                )
                # Attach decision metadata so the trade log surfaces which
                # pattern fired and at what conviction. Used for offline
                # analysis (per-pattern win-rate, conviction calibration, …);
                # not part of the broker contract so we attach lazily.
                setattr(sim_pos, "setup_type", getattr(decision, "setup_type", "") or "")
                setattr(sim_pos, "conviction", getattr(decision, "conviction", 0) or 0)
                self.broker.positions[symbol] = sim_pos

            # Record equity point
            self._equity_curve.append({
                "timestamp":    ts,
                "account_value": self.broker.account_value,
                "open_trades":  len(self.broker.positions),
            })

        return BacktestReport(
            trades        = self.broker.closed_trades,
            equity_curve  = pd.DataFrame(self._equity_curve),
            initial_value = self.broker.initial_account_value,
            final_value   = self.broker.account_value,
        )


# ═══════════════════════════════════════════════════════════════
#  BACKTEST REPORT
# ═══════════════════════════════════════════════════════════════

@dataclass
class BacktestReport:
    trades:       list[SimPosition]
    equity_curve: pd.DataFrame
    initial_value: float
    final_value:  float

    def print_summary(self) -> None:
        if not self.trades:
            print("No trades to report.")
            return

        total   = len(self.trades)
        winners = [t for t in self.trades if t.pnl > 0]
        losers  = [t for t in self.trades if t.pnl <= 0]
        win_rate = len(winners) / total if total else 0

        gross_profit = sum(t.pnl for t in winners)
        gross_loss   = abs(sum(t.pnl for t in losers)) or 1
        profit_factor = gross_profit / gross_loss

        pnl_pcts = [t.pnl_pct for t in self.trades]
        avg_win  = np.mean([t.pnl_pct for t in winners]) if winners else 0
        avg_loss = np.mean([t.pnl_pct for t in losers])  if losers  else 0

        # Max drawdown from equity curve
        eq = self.equity_curve["account_value"]
        peak = eq.expanding().max()
        dd   = ((eq - peak) / peak * 100)
        max_dd = float(dd.min())

        total_return = (self.final_value - self.initial_value) / self.initial_value * 100

        print(f"""
╔══ YUKTI BACKTEST RESULTS ══════════════════════════════════════╗
  Total trades     : {total}
  Win rate         : {win_rate:.1%}
  Profit factor    : {profit_factor:.2f}   (target: >1.5)
  Total return     : {total_return:+.1f}%
  Max drawdown     : {max_dd:.1f}%         (target: >-15%)
  Avg win          : {avg_win:+.2f}%
  Avg loss         : {avg_loss:+.2f}%
  Expectancy       : {win_rate*avg_win + (1-win_rate)*avg_loss:.2f}% per trade
  Final value      : ₹{self.final_value:,.0f}
╚════════════════════════════════════════════════════════════════╝
        """.strip())

    def to_csv(self, path: str) -> None:
        pd.DataFrame([
            {
                "symbol":      t.symbol,
                "direction":   t.direction,
                "entry":       t.entry_price,
                "exit":        t.exit_price,
                "qty":         t.original_quantity or t.quantity,
                "pnl":         round(t.pnl, 2),
                "pnl_pct":     round(t.pnl_pct, 4),
                "exit_reason": t.exit_reason,
                "entry_time":  t.entry_time,
                "exit_time":   t.exit_time,
                "setup":       getattr(t, "setup_type", ""),
                "conviction":  getattr(t, "conviction", 0),
            }
            for t in self.trades
        ]).to_csv(path, index=False)
        log.info("Saved trade log to %s", path)


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_cli() -> None:
    """Command-line interface for running backtests."""
    import argparse

    parser = argparse.ArgumentParser(description="Yukti Backtest Engine")
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="End date YYYY-MM-DD")
    parser.add_argument("--sample-rate", type=float, default=0.3, help="Claude call sample rate (0.0-1.0, AI path only)")
    parser.add_argument("--symbols", nargs="*", default=None, help="Specific symbols (default: all in universe)")
    parser.add_argument("--rules-engine", action="store_true", help="Use deterministic rules engine instead of AI")
    parser.add_argument("--interval", default=None, help="Candle interval to backtest (default: settings.candle_interval, e.g. 5 or D)")
    args = parser.parse_args()

    import asyncio
    asyncio.run(_run_backtest(args.start, args.end, args.sample_rate, args.symbols, args.rules_engine, args.interval))


async def _run_backtest(
    start: str,
    end: str,
    sample_rate: float,
    symbols: list[str] | None = None,
    use_rules_engine: bool = False,
    interval: str | None = None,
) -> None:
    """Run the backtest with optional symbol filter."""
    from yukti.config import settings
    from yukti.data.models import Candle
    from sqlalchemy import select, and_, func as sa_func
    import pandas as pd

    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    candle_interval = str(interval or settings.candle_interval).strip()
    daily_intervals = {"D", "1D", "d", "1d", "day", "daily"}
    normalized_interval = "D" if candle_interval in daily_intervals else candle_interval

    # Indicator warmup. 50-EMA needs ≥50 bars and the engine skips symbols with
    # <60 bars; without pre-roll a 1-month daily backtest would produce zero
    # trades because every symbol's slice has only ~22 candles.
    # The engine still iterates only over candles inside [start, end] — the
    # warmup tail is used purely to seed indicator state.
    warmup_days = 220 if normalized_interval == "D" else 14
    db_window_start = start_date - timedelta(days=warmup_days)

    # Load universe or use provided symbols
    if symbols:
        universe = symbols
    else:
        try:
            import redis.asyncio as aioredis
            r = await aioredis.from_url(settings.redis_url, decode_responses=True)
            raw = await r.get("yukti:universe")
            await r.aclose()
            if raw:
                universe_list = json.loads(raw)
                universe = [u["symbol"] for u in universe_list]
            else:
                universe = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"]
        except Exception:
            universe = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"]

    # Load candles — try DB first, fall back to yfinance (daily) when DB is unreachable.
    # The DB window includes warmup history (db_window_start..start_date) so
    # indicators have enough lookback before the first decision bar.
    candles: dict[str, pd.DataFrame] = {}
    try:
        from yukti.data.database import create_all_tables, get_db
        await create_all_tables()
        async with get_db() as db:
            for symbol in universe:
                rows = (await db.execute(
                    select(Candle)
                    .where(
                        and_(
                            Candle.symbol == symbol,
                            Candle.interval == normalized_interval,
                            sa_func.date(Candle.time) >= db_window_start,
                            sa_func.date(Candle.time) <= end_date,
                        )
                    )
                    .order_by(Candle.time)
                )).scalars().all()
                if rows:
                    df = pd.DataFrame(
                        [(r.time, r.open, r.high, r.low, r.close, r.volume) for r in rows],
                        columns=["time", "open", "high", "low", "close", "volume"],
                    ).set_index("time")
                    candles[symbol] = df.astype(float)
    except Exception as db_err:
        log.warning("DB unavailable (%s) — falling back to yfinance daily data", db_err)

    if not candles and normalized_interval == "D":
        try:
            import warnings
            import yfinance as yf
            warnings.filterwarnings("ignore")
            warmup_start = (
                datetime.fromisoformat(start) - timedelta(days=180)
            ).strftime("%Y-%m-%d")
            ns_syms = [s + ".NS" for s in universe]
            log.info("Fetching %d symbols from yfinance [%s → %s]", len(universe), warmup_start, end)
            raw = yf.download(ns_syms, start=warmup_start, end=end,
                              interval="1d", auto_adjust=True, progress=False)
            for sym, ns in zip(universe, ns_syms):
                try:
                    df = raw.loc[:, (slice(None), ns)].copy()
                    df.columns = df.columns.get_level_values(0).str.lower()
                    df.index = pd.to_datetime(df.index)
                    df = df.dropna(subset=["close"])
                    if len(df) >= 60:
                        candles[sym] = df.astype(float)
                except Exception:
                    pass
        except Exception as yf_err:
            log.error("yfinance fallback also failed: %s", yf_err)
    elif not candles:
        log.error(
            "No %s-minute candle data found in DB. Populate candles for interval=%s before running intraday backtests.",
            normalized_interval,
            normalized_interval,
        )

    if not candles:
        log.error("No candle data found. Populate the DB or install yfinance.")
        return

    nifty_df = candles.get("NIFTY", next(iter(candles.values())))
    engine = BacktestEngine(
        candles, nifty_df,
        account_value=settings.account_value,
        claude_sample_rate=sample_rate,
        use_rules_engine=use_rules_engine,
        decision_start=start_date,
    )
    report = await engine.run()
    report.print_summary()
    report.to_csv("backtest_trades.csv")
