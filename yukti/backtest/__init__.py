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
from datetime import date, datetime
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


class PaperBroker:
    """
    Simulates DhanHQ order execution for paper trading and backtesting.
    Trades are filled at the next available price (limit: at limit or better).
    GTT orders trigger when price crosses the trigger level.
    """

    def __init__(self, account_value: float = 500_000.0, slippage_pct: float = 0.001) -> None:
        self.account_value  = account_value
        self.slippage_pct   = slippage_pct          # 0.1% default slippage
        self.positions:     dict[str, SimPosition] = {}
        self.closed_trades: list[SimPosition]      = []
        self.order_counter  = 0
        self._current_prices: dict[str, float] = {}

    def update_prices(self, prices: dict[str, float]) -> None:
        """Called each candle with current close prices. Triggers GTT checks."""
        self._current_prices.update(prices)
        self._check_gtts()

    def _check_gtts(self) -> None:
        """Check if any position's SL or target has been hit."""
        for symbol, pos in list(self.positions.items()):
            if pos.status != "OPEN":
                continue
            price = self._current_prices.get(symbol)
            if not price:
                continue

            if pos.direction == "LONG":
                if price <= pos.stop_loss:
                    self._close_position(symbol, pos.stop_loss * (1 - self.slippage_pct), "stop_loss_hit")
                elif price >= pos.target_1:
                    self._close_position(symbol, pos.target_1, "target_1_hit")
            else:  # SHORT
                if price >= pos.stop_loss:
                    self._close_position(symbol, pos.stop_loss * (1 + self.slippage_pct), "stop_loss_hit")
                elif price <= pos.target_1:
                    self._close_position(symbol, pos.target_1, "target_1_hit")

    def _close_position(self, symbol: str, exit_price: float, reason: str) -> None:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return
        is_long = pos.direction == "LONG"
        pos.exit_price  = exit_price
        pos.exit_reason = reason
        pos.pnl         = (exit_price - pos.entry_price) * pos.quantity if is_long else (pos.entry_price - exit_price) * pos.quantity
        pos.pnl_pct     = pos.pnl / (pos.entry_price * pos.quantity) * 100
        pos.status      = "CLOSED"
        self.account_value += pos.pnl
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
        self._close_position(security_id, price, "eod_squareoff")
        return {"orderId": f"EXIT-{self.order_counter}", "status": "TRADED"}


# ═══════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
#  Replays historical candles through the full Yukti agent
# ═══════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Replays historical OHLCV candles symbol-by-symbol, cycle-by-cycle.
    On each candle close, runs the full Yukti signal + Claude pipeline.
    Uses PaperBroker for simulated fills.
    """

    def __init__(
        self,
        candles:       dict[str, pd.DataFrame],  # symbol → OHLCV df
        nifty_candles: pd.DataFrame,
        account_value: float       = 500_000.0,
        claude_sample_rate: float  = 1.0,   # 1.0=always, 0.1=10% sample (cheap testing)
    ) -> None:
        self.candles            = candles
        self.nifty_candles      = nifty_candles
        self.broker             = PaperBroker(account_value)
        self.claude_sample_rate = claude_sample_rate
        self._equity_curve:     list[dict] = []

    async def run(self) -> "BacktestReport":
        """Run the full backtest and return a report."""
        from yukti.agents.arjun import arjun
        from yukti.signals.indicators import compute
        from yukti.signals.context import build_context
        from yukti.risk import calculate_position, calculate_levels, run_gates
        from yukti.execution.order_sm import open_trade

        symbols    = list(self.candles.keys())
        all_dates  = sorted(set(
            idx for df in self.candles.values() for idx in df.index
        ))

        log.info("Backtest: %d symbols × %d candles", len(symbols), len(all_dates))

        for ts in all_dates:
            nifty_slice = self.nifty_candles.loc[:ts]
            if len(nifty_slice) < 50:
                continue

            nifty_chg   = float((nifty_slice["close"].iloc[-1] - nifty_slice["close"].iloc[-2]) / nifty_slice["close"].iloc[-2] * 100)
            nifty_trend = "UP" if nifty_slice["close"].iloc[-1] > nifty_slice["close"].iloc[-10] else "DOWN"

            # Update paper broker prices at this timestamp
            prices = {}
            for sym, df in self.candles.items():
                if ts in df.index:
                    prices[sym] = float(df.loc[ts, "close"])
            self.broker.update_prices(prices)

            for symbol in symbols:
                df = self.candles[symbol].loc[:ts]
                if len(df) < 60:
                    continue

                # Sample rate: skip some candles to reduce Claude API calls
                import random
                if random.random() > self.claude_sample_rate:
                    continue

                try:
                    snap = compute(df)
                except Exception:
                    continue

                perf = {
                    "consecutive_losses": 0,
                    "daily_pnl_pct":      0.0,
                    "win_rate_last_10":   0.5,
                    "trades_today":       0,
                }

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

                # Fill levels from Claude or compute fallback
                if not decision.stop_loss or not decision.target_1:
                    levels = calculate_levels(
                        decision.direction or "LONG",
                        decision.entry_price or snap.close,
                        snap.atr,
                        snap.nearest_swing_low,
                        snap.nearest_swing_high,
                    )
                    decision.stop_loss  = decision.stop_loss  or levels.stop_loss
                    decision.target_1   = decision.target_1   or levels.target_1
                    decision.target_2   = decision.target_2   or levels.target_2
                    decision.risk_reward = decision.risk_reward or levels.risk_reward

                try:
                    position = calculate_position(
                        entry_price  = decision.entry_price or snap.close,
                        stop_loss    = decision.stop_loss,
                        direction    = decision.direction or "LONG",
                        conviction   = decision.conviction,
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
                )
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
            initial_value = 500_000.0,
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
                "qty":         t.quantity,
                "pnl":         round(t.pnl, 2),
                "pnl_pct":     round(t.pnl_pct, 4),
                "exit_reason": t.exit_reason,
                "entry_time":  t.entry_time,
            }
            for t in self.trades
        ]).to_csv(path, index=False)
        log.info("Saved trade log to %s", path)
