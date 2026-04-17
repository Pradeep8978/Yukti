"""
yukti/services/market_scan_service.py
Handles market scanning and signal processing.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict

from yukti.agents.arjun import arjun
from yukti.agents.memory import retrieve_similar
from yukti.data.state import is_halted, get_performance_state, get_daily_pnl_pct, count_open_positions
from yukti.execution.dhan_client import dhan
from yukti.execution.order_sm import open_trade
from yukti.metrics import signals_scanned, record_skip, record_trade_opened
from yukti.risk import calculate_levels, calculate_position, run_gates, Portfolio
from yukti.scheduler.jobs import is_trading_day, is_trading_hours
from yukti.signals.context import build_context
from yukti.signals.indicators import compute
from yukti.signals.patterns import best_pattern
from yukti.watchdog import heartbeat

from yukti.config import settings

log = logging.getLogger(__name__)


class MarketScanService:
    def __init__(self, universe: Dict[str, str]) -> None:
        self.universe = universe
        self.interval_secs = 300  # 5 min
        self.max_concurrent = 5
        self.sem = asyncio.Semaphore(self.max_concurrent)

    async def run_single_scan(self) -> None:
        """Run one complete scan cycle (for paper mode)."""
        log.info("MarketScanService: starting single scan cycle")

        nifty_chg, nifty_trend = await self._get_nifty_context()
        perf = await get_performance_state()

        for symbol, security_id in self.universe.items():
            if await is_halted():
                log.info("MarketScanService: halted, stopping scan")
                break
            await self._scan_symbol(symbol, security_id, nifty_chg, nifty_trend, perf)

        log.info("MarketScanService: single scan cycle complete")

    async def run_continuous_scan(self) -> None:
        """Run continuous scanning loop (for live mode)."""
        log.info("MarketScanService: starting continuous scan loop")

        while True:
            cycle_start = asyncio.get_event_loop().time()

            if await is_halted():
                await asyncio.sleep(30)
                continue

            if not is_trading_day() or not is_trading_hours():
                heartbeat()
                await asyncio.sleep(30)
                continue

            try:
                nifty_chg, nifty_trend = await self._get_nifty_context()
                perf = await get_performance_state()

                tasks = [
                    self._scan_symbol(symbol, security_id, nifty_chg, nifty_trend, perf)
                    for symbol, security_id in self.universe.items()
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                failed_scans = sum(1 for r in results if isinstance(r, Exception))
                if failed_scans > 0:
                    log.warning("MarketScanService: cycle completed with %d/%d failed scans", failed_scans, len(self.universe))

                heartbeat()

            except Exception as exc:
                log.error("MarketScanService: cycle error: %s", exc)

            elapsed = asyncio.get_event_loop().time() - cycle_start
            await asyncio.sleep(max(5, self.interval_secs - elapsed))

    async def _get_nifty_context(self) -> tuple[float, str]:
        """Get Nifty change and trend."""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
            nifty_raw = await dhan.get_candles("13", 5, start, today)
            if not nifty_raw or len(nifty_raw) < 20:
                return 0.0, "SIDEWAYS"
            nifty_df = pd.DataFrame(nifty_raw, columns=["time", "open", "high", "low", "close", "volume"]).astype({"close": float})
            nifty_chg = float((nifty_df["close"].iloc[-1] - nifty_df["close"].iloc[-2]) / nifty_df["close"].iloc[-2] * 100)
            nifty_trend = "UP" if nifty_df["close"].iloc[-1] > nifty_df["close"].iloc[-10] else "DOWN"
            return nifty_chg, nifty_trend
        except Exception as exc:
            log.warning("MarketScanService: Nifty fetch failed: %s", exc)
            return 0.0, "SIDEWAYS"

    async def _scan_symbol(self, symbol: str, security_id: str, nifty_chg: float, nifty_trend: str, perf: dict) -> None:
        """Scan one symbol."""
        async with self.sem:
            signals_scanned.inc()
            log.info("MarketScanService: scanning %s", symbol)
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
                raw = await dhan.get_candles(security_id, 5, start, today)
                if not raw or len(raw) < 60:
                    return

                df = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"]).astype({c: float for c in ["open","high","low","close","volume"]})
                snap = compute(df)

                past_journal = await retrieve_similar(symbol, "unknown", "LONG")
                context = build_context(symbol, snap, nifty_chg, nifty_trend, "No breaking news", perf, past_journal)

                decision = await arjun.safe_decide(context)
                log.info("MarketScanService: AI decision for %s: %s (conviction %d)", symbol, decision.action, decision.conviction)

                if decision.action == "SKIP":
                    record_skip(decision.skip_reason or "claude_skip")
                    return

                if not decision.stop_loss or not decision.target_1:
                    levels = calculate_levels(decision.direction or "LONG", decision.entry_price or snap.close, snap.atr, snap.nearest_swing_low, snap.nearest_swing_high)
                    decision.stop_loss = decision.stop_loss or levels.stop_loss
                    decision.target_1 = decision.target_1 or levels.target_1
                    decision.target_2 = decision.target_2 or levels.target_2
                    decision.risk_reward = decision.risk_reward or levels.risk_reward

                portfolio = Portfolio(
                    account_value=settings.account_value,
                    open_positions=await count_open_positions(),
                    daily_pnl_pct=await get_daily_pnl_pct(),
                    total_exposure_pct=0.0,
                )
                gate = await run_gates(decision, portfolio)
                if not gate.passed:
                    record_skip(gate.reason or "gate_blocked")
                    log.info("MarketScanService: risk gate failed for %s: %s", symbol, gate.reason)
                    return

                position = calculate_position(decision.entry_price or snap.close, decision.stop_loss, decision.direction or "LONG", decision.conviction)
                pos = await open_trade(symbol, security_id, decision, position)
                if pos:
                    record_trade_opened(decision.direction or "LONG", decision.setup_type or "unknown")

            except Exception as exc:
                log.error("MarketScanService: scan error %s: %s", symbol, exc)