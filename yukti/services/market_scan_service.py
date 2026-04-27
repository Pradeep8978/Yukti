"""
yukti/services/market_scan_service.py
Handles market scanning and signal processing.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict

import pandas as pd

from yukti.agents.arjun import arjun
from yukti.agents.memory import retrieve_similar_trades, format_retrieved_journals_for_context
from yukti.config import settings
from yukti.data.state import (
    is_halted,
    get_performance_state,
    get_daily_pnl_pct,
    count_open_positions,
    get_all_positions,
)
from yukti.execution.dhan_client import dhan
from yukti.execution.order_sm import open_trade
from yukti.metrics import signals_scanned, scan_failures, record_skip, record_trade_opened
from yukti.risk import calculate_levels, calculate_position, run_gates, Portfolio
from yukti.scheduler.jobs import is_trading_day, is_trading_hours
from yukti.services.macro_context_service import MacroContext, fetch_macro_context, filter_headlines_for_symbol
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

        macro = await self._get_macro_context()
        perf = await get_performance_state()

        for symbol, security_id in self.universe.items():
            if await is_halted():
                log.info("MarketScanService: halted, stopping scan")
                break
            await self._scan_symbol(symbol, security_id, macro, perf)

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
                macro = await self._get_macro_context()
                perf = await get_performance_state()

                tasks = [
                    self._scan_symbol(symbol, security_id, macro, perf)
                    for symbol, security_id in self.universe.items()
                ]
                symbols_list = list(self.universe.keys())
                results = await asyncio.gather(*tasks, return_exceptions=True)
                failed_scans = 0
                for sym, result in zip(symbols_list, results):
                    if isinstance(result, Exception):
                        failed_scans += 1
                        scan_failures.labels(symbol=sym).inc()
                        log.error("MarketScanService: scan failed for %s: %s", sym, result, exc_info=result)
                if failed_scans > 0:
                    log.warning("MarketScanService: cycle completed with %d/%d failed scans", failed_scans, len(self.universe))

                heartbeat()

            except Exception as exc:
                log.error("MarketScanService: cycle error: %s", exc)

            elapsed = asyncio.get_event_loop().time() - cycle_start
            await asyncio.sleep(max(5, self.interval_secs - elapsed))

    async def _get_macro_context(self) -> MacroContext:
        """Fetch Nifty data then assemble full MacroContext (VIX, FII/DII, headlines)."""
        nifty_chg, nifty_trend = 0.0, "SIDEWAYS"
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
            nifty_raw = await dhan.get_candles("13", 5, start, today, symbol="NIFTY")
            if nifty_raw and len(nifty_raw) >= 20:
                nifty_df = pd.DataFrame(nifty_raw, columns=["time", "open", "high", "low", "close", "volume"])
                nifty_df["time"] = pd.to_datetime(nifty_df["time"])
                nifty_df.set_index("time", inplace=True)
                nifty_df.sort_index(inplace=True)
                nifty_df = nifty_df.astype({"close": float})
                nifty_chg = float((nifty_df["close"].iloc[-1] - nifty_df["close"].iloc[-2]) / nifty_df["close"].iloc[-2] * 100)
                nifty_trend = "UP" if nifty_df["close"].iloc[-1] > nifty_df["close"].iloc[-10] else "DOWN"
                # Cache Nifty change for circuit-breaker gate
                from yukti.data.state import get_redis
                r = await get_redis()
                await r.set("yukti:market:nifty_chg_pct", str(nifty_chg), ex=600)
        except Exception as exc:
            log.warning("MarketScanService: Nifty fetch failed: %s", exc)

        return await fetch_macro_context(nifty_chg, nifty_trend)

    async def _get_daily_candles(self, symbol: str, security_id: str) -> pd.DataFrame | None:
        """
        Fetch 60-day daily candles, cached in Redis for one trading session.
        Returns DataFrame or None on failure.
        """
        from yukti.data.state import get_redis
        cache_key = f"yukti:daily_candles:{symbol}"
        r = await get_redis()

        # Check cache
        cached = await r.get(cache_key)
        if cached:
            data = json.loads(cached)
            df = pd.DataFrame(data)
            if len(df) >= 20:
                return df

        # Fetch from DhanHQ
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=settings.daily_candle_history + 10)).strftime("%Y-%m-%d")
            raw = await dhan.get_candles(security_id, "1", start, today, symbol=symbol)
            if not raw or len(raw) < 20:
                return None

            df = pd.DataFrame(
                raw, columns=["time", "open", "high", "low", "close", "volume"]
            )
            df["time"] = pd.to_datetime(df["time"])
            df.set_index("time", inplace=True)
            df.sort_index(inplace=True)
            df = df.astype({c: float for c in ["open", "high", "low", "close", "volume"]})

            # Cache with session TTL
            await r.set(cache_key, json.dumps(df.to_dict("records")), ex=settings.daily_cache_ttl)
            log.debug("Cached daily candles for %s (%d rows)", symbol, len(df))
            return df
        except Exception as exc:
            log.warning("Failed to fetch daily candles for %s: %s", symbol, exc)
            return None

    async def _scan_symbol(self, symbol: str, security_id: str, macro: MacroContext, perf: dict) -> None:
        """Scan one symbol with daily + 5-min multi-timeframe analysis."""
        async with self.sem:
            signals_scanned.inc()
            log.info("MarketScanService: scanning %s", symbol)
            try:
                # ── 5-min candles (existing) ──────────────────────────
                today = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
                raw = await dhan.get_candles(security_id, 5, start, today, symbol=symbol)
                if not raw or len(raw) < 60:
                    return

                df = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])
                df["time"] = pd.to_datetime(df["time"])
                df.set_index("time", inplace=True)
                df.sort_index(inplace=True)
                df = df.astype({c: float for c in ["open","high","low","close","volume"]})
                snap = compute(df, timeframe="5m")

                # ── Daily candles (new — multi-timeframe) ─────────────
                snap_daily = None
                daily_df = await self._get_daily_candles(symbol, security_id)
                if daily_df is not None and len(daily_df) >= 20:
                    snap_daily = compute(daily_df, timeframe="daily")

                # ── Current time for time-gating ──────────────────────
                current_time = datetime.now().time()

                # ── ORB levels (from first 3 candles of today) ────────
                or_high, or_low = None, None
                if len(df) >= 3:
                    or_candles = df.iloc[:3]
                    or_high = float(or_candles["high"].max())
                    or_low = float(or_candles["low"].min())

                # ── Pattern detection (updated with multi-timeframe) ──
                pattern = best_pattern(snap, candles=df, indicators_daily=snap_daily, current_time=current_time)

                # ── Memory retrieval (hybrid) ───────────────────────────
                memory_setup = pattern.pattern_type if pattern else "unknown"
                memory_dir   = "LONG" if macro.nifty_trend == "UP" else "SHORT" if macro.nifty_trend == "DOWN" else "LONG"
                # Map macro trend to market regime
                regime_map = {"UP": "BULLISH", "DOWN": "BEARISH", "SIDEWAYS": "NEUTRAL"}
                market_regime = regime_map.get(macro.nifty_trend, "NEUTRAL")
                
                # Use hybrid retrieval with metadata filters
                retrieved_journals = await retrieve_similar_trades(
                    symbol, memory_setup, memory_dir, market_regime=market_regime, top_k=settings.rag_max_retrieved
                )
                past_journal = format_retrieved_journals_for_context(
                    retrieved_journals, 
                    include_meta_lessons=settings.rag_include_meta_lessons
                )
                symbol_headlines = filter_headlines_for_symbol(symbol, macro.headlines)

                # ── Context (updated with daily + ORB/VWAP) ──────────
                context = build_context(
                    symbol, snap, macro, perf, past_journal, symbol_headlines,
                    indicators_daily=snap_daily,
                    or_high=or_high,
                    or_low=or_low,
                )

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

                # Compute total exposure as sum(entry_price * quantity) / account_value
                all_positions = await get_all_positions()
                total_notional = 0.0
                for p in all_positions.values():
                    try:
                        total_notional += float(p.get("entry_price", 0)) * int(p.get("quantity", 0))
                    except Exception:
                        continue

                total_exposure_pct = (
                    round(total_notional / settings.account_value * 100, 2)
                    if settings.account_value
                    else 0.0
                )

                portfolio = Portfolio(
                    account_value=settings.account_value,
                    open_positions=await count_open_positions(),
                    daily_pnl_pct=await get_daily_pnl_pct(),
                    total_exposure_pct=total_exposure_pct,
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
                    try:
                        from yukti.telegram.bot import alert_trade_opened
                        await alert_trade_opened(pos)
                    except Exception as tg_exc:
                        log.warning("Telegram trade alert failed: %s", tg_exc)

            except Exception as exc:
                scan_failures.labels(symbol=symbol).inc()
                log.error("MarketScanService: scan error %s: %s", symbol, exc, exc_info=True)