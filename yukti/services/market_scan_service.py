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
    is_on_cooldown,
    get_performance_state,
    get_daily_pnl_pct,
    count_open_positions,
    get_all_positions,
)
from yukti.execution.broker_factory import get_broker
from yukti.execution.order_sm import open_trade
from yukti.metrics import signals_scanned, scan_failures, record_skip, record_trade_opened
from yukti.risk import calculate_levels, calculate_position, run_gates, Portfolio, is_market_halted
from yukti.scheduler.jobs import KOLKATA, is_trading_day, is_trading_hours
from yukti.services.balance_service import fetch_available_balance
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
        # Serialises the check-then-open step so concurrent scans can't both
        # pass the MAX_OPEN_POSITIONS gate and double-enter the same cycle.
        self._open_lock = asyncio.Lock()

    async def _get_cycle_universe(self) -> list[tuple[str, str]]:
        """Return the full shortlisted universe for scanning every cycle.

        Open positions are always first so existing trades are monitored before
        new entries are considered. The semaphore (max_concurrent) already caps
        parallel broker calls — no need to rotate a subset across cycles.

        Re-reads Redis each cycle so universe updates from scheduler jobs
        (job_universe_scan, job_universe_refresh) take effect without a restart.
        Falls back to the startup-loaded self.universe when Redis is empty.
        """
        import json as _json
        import redis.asyncio as _aioredis
        try:
            _r = await _aioredis.from_url(settings.redis_url, decode_responses=True)
            _raw = await _r.get("yukti:universe")
            await _r.aclose()
            if _raw:
                _entries = _json.loads(_raw)
                if _entries:
                    self.universe = {u["symbol"]: u["security_id"] for u in _entries}
        except Exception as _exc:
            log.debug("_get_cycle_universe: Redis re-read failed: %s", _exc)

        universe_items = list(self.universe.items())
        positions = await get_all_positions()

        # Open positions first, then the rest in universe order
        selected: list[tuple[str, str]] = []
        seen_symbols: set[str] = set()

        for symbol, security_id in universe_items:
            if symbol in positions and symbol not in seen_symbols:
                selected.append((symbol, security_id))
                seen_symbols.add(symbol)

        for symbol, security_id in universe_items:
            if symbol not in seen_symbols:
                selected.append((symbol, security_id))

        return selected

    async def run_single_scan(self) -> None:
        """Run one complete scan cycle (for paper mode)."""
        log.info("MarketScanService: starting single scan cycle")

        macro = await self._get_macro_context()
        perf = await get_performance_state()
        live_account_value = await fetch_available_balance()

        cycle_universe = await self._get_cycle_universe()

        for symbol, security_id in cycle_universe:
            if await is_halted():
                log.info("MarketScanService: halted, stopping scan")
                break
            await self._scan_symbol(symbol, security_id, macro, perf, live_account_value)

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
                live_account_value = await fetch_available_balance()
                cycle_universe = await self._get_cycle_universe()

                tasks = [
                    self._scan_symbol(symbol, security_id, macro, perf, live_account_value)
                    for symbol, security_id in cycle_universe
                ]
                symbols_list = [symbol for symbol, _ in cycle_universe]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                failed_scans = 0
                for sym, result in zip(symbols_list, results):
                    if isinstance(result, Exception):
                        failed_scans += 1
                        scan_failures.labels(symbol=sym).inc()
                        log.error("MarketScanService: scan failed for %s: %s", sym, result, exc_info=result)
                if failed_scans > 0:
                    log.warning("MarketScanService: cycle completed with %d/%d failed scans", failed_scans, len(cycle_universe))
                    if len(cycle_universe) > 0 and failed_scans / len(cycle_universe) >= 0.3:
                        try:
                            from yukti.telegram.bot import alert_scan_error
                            await alert_scan_error("High scan failure rate", failed_scans, len(cycle_universe))
                        except Exception:
                            pass

                heartbeat()

            except Exception as exc:
                log.error("MarketScanService: cycle error: %s", exc)
                try:
                    from yukti.telegram.bot import alert_scan_error
                    await alert_scan_error(str(exc))
                except Exception:
                    pass

            elapsed = asyncio.get_event_loop().time() - cycle_start
            try:
                _open = await count_open_positions()
                _pnl  = await get_daily_pnl_pct()
                _tt   = int((await get_performance_state()).get("trades_today", 0) or 0)
                log.info(
                    "CYCLE | symbols=%d elapsed=%.1fs | open=%d trades_today=%d daily_pnl=%.2f%%",
                    len(cycle_universe), elapsed, _open, _tt, _pnl,
                )
            except Exception:
                pass
            await asyncio.sleep(max(5, self.interval_secs - elapsed))

    async def _get_macro_context(self) -> MacroContext:
        """Fetch Nifty data then assemble full MacroContext (VIX, FII/DII, headlines)."""
        broker = get_broker()
        nifty_chg, nifty_trend = 0.0, "SIDEWAYS"
        try:
            now_ist = datetime.now(KOLKATA)
            today = now_ist.strftime("%Y-%m-%d")
            start = (now_ist - timedelta(days=3)).strftime("%Y-%m-%d")
            nifty_raw = await broker.get_candles("13", 5, start, today, symbol="NIFTY")
            if nifty_raw and len(nifty_raw) >= 20:
                nifty_df = pd.DataFrame(nifty_raw, columns=["time", "open", "high", "low", "close", "volume"])
                nifty_df["time"] = pd.to_datetime(nifty_df["time"])
                nifty_df.set_index("time", inplace=True)
                nifty_df.sort_index(inplace=True)
                nifty_df = nifty_df.astype({"close": float})
                nifty_chg = float((nifty_df["close"].iloc[-1] - nifty_df["close"].iloc[-2]) / nifty_df["close"].iloc[-2] * 100)
                nifty_trend = "UP" if nifty_df["close"].iloc[-1] > nifty_df["close"].iloc[-10] else "DOWN"
                # Cache Nifty change for circuit-breaker gate. Stored as JSON
                # with a write timestamp so the consumer can reject stale data
                # even if (somehow) a long TTL is set elsewhere.
                import time as _time
                from yukti.data.state import get_redis
                r = await get_redis()
                await r.set(
                    "yukti:market:nifty_chg_pct",
                    json.dumps({"chg_pct": nifty_chg, "ts": int(_time.time())}),
                    ex=600,
                )
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
            broker = get_broker()
            now_ist = datetime.now(KOLKATA)
            today = now_ist.strftime("%Y-%m-%d")
            start = (now_ist - timedelta(days=settings.daily_candle_history + 10)).strftime("%Y-%m-%d")
            raw = await broker.get_candles(security_id, "1", start, today, symbol=symbol)
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

    async def _scan_symbol(self, symbol: str, security_id: str, macro: MacroContext, perf: dict, live_account_value: float = 0.0) -> None:
        """Scan one symbol with daily + 5-min multi-timeframe analysis."""
        async with self.sem:
            signals_scanned.inc()
            log.info("MarketScanService: scanning %s", symbol)
            try:
                broker = get_broker()
                # ── 5-min candles (existing) ──────────────────────────
                now_ist = datetime.now(KOLKATA)
                today = now_ist.strftime("%Y-%m-%d")
                start = (now_ist - timedelta(days=3)).strftime("%Y-%m-%d")
                raw = await broker.get_candles(security_id, 5, start, today, symbol=symbol)
                if not raw or len(raw) < 60:
                    return

                df = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])
                df["time"] = pd.to_datetime(df["time"])
                df.set_index("time", inplace=True)
                df.sort_index(inplace=True)
                df = df.astype({c: float for c in ["open","high","low","close","volume"]})
                snap = compute(df, timeframe="5m")
                log.info(
                    "SNAP %s | close=%.2f vwap=%.2f ema20=%.2f ema50=%.2f "
                    "rsi=%.1f macd_hist=%.4f atr=%.2f vol_ratio=%.2fx "
                    "trend=%s st=%s bb_pos=%.0f%%",
                    symbol,
                    snap.close, snap.vwap, snap.ema20, snap.ema50,
                    snap.rsi, snap.macd_hist, snap.atr, snap.volume_ratio,
                    snap.trend, "BULL" if snap.supertrend_bull else "BEAR",
                    (snap.close - snap.bb_lower) / max(snap.bb_upper - snap.bb_lower, 0.01) * 100,
                )

                # ── Affordability pre-filter: skip before AI if we can't buy 1 share ──
                _margin_per_share = snap.close / settings.intraday_leverage
                if _margin_per_share > live_account_value:
                    record_skip("unaffordable")
                    log.info(
                        "SKIP %s | unaffordable: margin/share ₹%.0f > account ₹%.0f",
                        symbol, _margin_per_share, live_account_value,
                    )
                    return

                # ── Daily candles (new — multi-timeframe) ─────────────
                snap_daily = None
                daily_df = await self._get_daily_candles(symbol, security_id)
                if daily_df is not None and len(daily_df) >= 20:
                    snap_daily = compute(daily_df, timeframe="daily")

                # ── Current time for time-gating ──────────────────────
                current_time = now_ist.time()
                today_df = df[df.index.date == now_ist.date()]
                pattern_df = today_df if len(today_df) >= 3 else None

                # ── ORB levels (from first 3 candles of today) ────────
                or_high, or_low = None, None
                if len(today_df) >= 3:
                    or_candles = today_df.iloc[:3]
                    or_high = float(or_candles["high"].max())
                    or_low = float(or_candles["low"].min())

                # ── Pattern detection (updated with multi-timeframe) ──
                pattern = best_pattern(snap, candles=pattern_df, indicators_daily=snap_daily, current_time=current_time)

                # ── Pre-AI filter: skip expensive AI call when signal is absent ──
                if pattern is None:
                    log.info("PATTERN %s | no signal (trend=%s rsi=%.1f vol_ratio=%.2fx)", symbol, snap.trend, snap.rsi, snap.volume_ratio)
                    record_skip("no_pattern")
                    return
                log.info("PATTERN %s | %s strength=%.2f | %s", symbol, pattern.pattern_type, pattern.strength, pattern.notes)

                if await is_halted() or await is_market_halted():
                    record_skip("market_halted_preai")
                    return

                _daily_pnl = await get_daily_pnl_pct()
                if _daily_pnl <= -(settings.daily_loss_limit_pct * 100):
                    record_skip("daily_loss_limit_preai")
                    return

                if await is_on_cooldown(symbol):
                    record_skip("cooldown_preai")
                    return

                # ── Liquidity gate: skip illiquid symbols before AI call ───
                # Fail-open: depth API errors never block a trade.
                try:
                    depth = await broker.get_market_depth(security_id)
                    if depth:
                        if depth.get("spread_pct", 0) > 0.15:
                            record_skip("illiquid_spread")
                            log.info("MarketScanService: %s skipped — spread %.3f%% > 0.15%%", symbol, depth["spread_pct"])
                            return
                        top_of_book = min(
                            depth.get("best_bid_qty", 999),
                            depth.get("best_ask_qty", 999),
                        )
                        if top_of_book < 200:
                            record_skip("illiquid_thin_book")
                            log.info("MarketScanService: %s skipped — thin book qty=%d", symbol, top_of_book)
                            return
                except Exception:
                    pass

                if not is_trading_day() or not is_trading_hours():
                    record_skip("outside_trading_hours")
                    log.info("MarketScanService: skipping decision for %s outside trading hours", symbol)
                    return

                # Block new entries < 20 min before EOD squareoff
                # A trade at 14:56 has only 19 minutes before forced close — not enough.
                eod_h, eod_m = (int(p) for p in settings.eod_squareoff.split(":"))
                _eod_dt = now_ist.replace(hour=eod_h, minute=eod_m, second=0, microsecond=0)
                _entry_cutoff = (_eod_dt - timedelta(minutes=20)).time()
                if current_time >= _entry_cutoff:
                    record_skip("too_close_to_eod")
                    return

                if not settings.use_ai_decision:
                    # ── Rules engine path (no API calls) ─────────────
                    from yukti.agents.rules_engine import decide as rules_decide
                    decision, _ = rules_decide(symbol, snap, macro, perf, pattern, snap_daily)
                    decision_label = "Rules"
                else:
                    # ── AI path (Gemini / Claude) ─────────────────────
                    # Memory direction derived from pattern type, not Nifty trend.
                    _long_patterns = {"breakout", "trend_pullback", "reversal_long", "momentum", "orb_breakout"}
                    memory_setup  = pattern.pattern_type if pattern else "unknown"
                    memory_dir    = "LONG" if pattern and pattern.pattern_type in _long_patterns else "SHORT"
                    regime_map    = {"UP": "BULLISH", "DOWN": "BEARISH", "SIDEWAYS": "NEUTRAL"}
                    market_regime = regime_map.get(macro.nifty_trend, "NEUTRAL")

                    retrieved_journals = await retrieve_similar_trades(
                        symbol, memory_setup, memory_dir, market_regime=market_regime, top_k=settings.rag_max_retrieved
                    )
                    past_journal = format_retrieved_journals_for_context(
                        retrieved_journals,
                        include_meta_lessons=settings.rag_include_meta_lessons
                    )
                    symbol_headlines = filter_headlines_for_symbol(symbol, macro.headlines)
                    context = build_context(
                        symbol, snap, macro, perf, past_journal, symbol_headlines,
                        indicators_daily=snap_daily,
                        or_high=or_high,
                        or_low=or_low,
                    )
                    decision = await arjun.safe_decide(context)
                    decision_label = "AI"

                reasoning_excerpt = " ".join((decision.reasoning or "").split())
                if len(reasoning_excerpt) > 240:
                    reasoning_excerpt = reasoning_excerpt[:237] + "..."
                log.info(
                    "MarketScanService: %s decision for %s: %s dir=%s conviction=%d bias=%s skip_reason=%s reasoning=%s",
                    decision_label,
                    symbol,
                    decision.action,
                    decision.direction or "-",
                    decision.conviction,
                    decision.market_bias,
                    decision.skip_reason or "-",
                    reasoning_excerpt or "-",
                )

                # Persist every AI decision for audit and quality analysis.
                # Best-effort — a DB failure must never block a trade.
                try:
                    from yukti.data.database import get_db
                    from yukti.data.models import DecisionLog
                    async with get_db() as _db:
                        _db.add(DecisionLog(
                            symbol=symbol,
                            action=decision.action,
                            direction=decision.direction,
                            market_bias=decision.market_bias,
                            conviction=decision.conviction,
                            reasoning=decision.reasoning or "",
                            skip_reason=decision.skip_reason,
                            full_json=decision.model_dump(),
                        ))
                        await _db.commit()
                except Exception as _dl_exc:
                    log.debug("decision_log insert failed (non-fatal): %s", _dl_exc)

                if decision.action == "SKIP":
                    record_skip(decision.skip_reason or "claude_skip")
                    return

                if not decision.stop_loss or not decision.target_1:
                    levels = calculate_levels(decision.direction or "LONG", decision.entry_price or snap.close, snap.atr, snap.nearest_swing_low, snap.nearest_swing_high)
                    decision.stop_loss = decision.stop_loss or levels.stop_loss
                    decision.target_1 = decision.target_1 or levels.target_1
                    decision.target_2 = decision.target_2 or levels.target_2
                    decision.risk_reward = decision.risk_reward or levels.risk_reward

                # Guard: zero balance means no funds available — skip sizing
                if live_account_value == 0:
                    record_skip("zero_balance")
                    return

                # Compute total exposure as margin-adjusted sum (notional / leverage per position)
                all_positions = await get_all_positions()
                total_notional = 0.0
                for p in all_positions.values():
                    try:
                        p_leverage = (
                            settings.intraday_leverage
                            if p.get("holding_period") == "intraday"
                            else 1.0
                        )
                        notional = float(p.get("entry_price", 0)) * int(p.get("quantity", 0))
                        total_notional += notional / p_leverage
                    except Exception:
                        continue

                total_exposure_pct = (
                    round(total_notional / live_account_value * 100, 2)
                    if live_account_value
                    else 0.0
                )

                regime_min_conviction = settings.min_conviction
                try:
                    from yukti.services.regime_service import load as load_regime
                    regime = await load_regime()
                    regime_min_conviction = min(10, settings.min_conviction + int(regime.conviction_bump or 0))
                except Exception as exc:
                    log.debug("Regime load skipped for gates: %s", exc)

                sector_exposure_pct, trade_sector = self._sector_exposure(all_positions, symbol, live_account_value)

                portfolio = Portfolio(
                    account_value=live_account_value,
                    open_positions=await count_open_positions(),
                    daily_pnl_pct=await get_daily_pnl_pct(),
                    total_exposure_pct=total_exposure_pct,
                    trades_today=int(perf.get("trades_today", 0) or 0),
                    min_conviction_override=regime_min_conviction,
                    sector_exposure_pct=sector_exposure_pct,
                    trade_sector=trade_sector,
                    win_rate_last_10=perf.get("win_rate_last_10"),
                    win_rate_last_10_count=int(perf.get("win_rate_last_10_count", 0) or 0),
                )
                gate = await run_gates(decision, portfolio)
                if not gate.passed:
                    record_skip(gate.reason or "gate_blocked")
                    log.info("MarketScanService: risk gate failed for %s: %s", symbol, gate.reason)
                    return

                _leverage = settings.intraday_leverage if decision.holding_period == "intraday" else 1.0
                position = calculate_position(
                    decision.entry_price or snap.close,
                    decision.stop_loss,
                    decision.direction or "LONG",
                    decision.conviction,
                    account_value=live_account_value,
                    leverage=_leverage,
                )
                log.info(
                    "SIZING %s | qty=%d (base=%d ×%.1f) risk=₹%.0f stop=₹%.2f "
                    "capital=₹%.0f margin=₹%.0f (%.1f%% of acct) leverage=%.0fx",
                    symbol,
                    position.quantity, position.base_quantity, position.conviction_multiplier,
                    float(position.risk_amount), float(position.stop_distance),
                    float(position.capital_deployed), float(position.margin_deployed),
                    float(position.capital_pct), _leverage,
                )
                # Hold the lock while opening so concurrent scans that all passed
                # gates on the same stale position count can't all enter at once.
                async with self._open_lock:
                    if await count_open_positions() >= settings.max_open_positions:
                        record_skip("max_positions_race")
                        log.info("MarketScanService: %s skipped — position slot taken by concurrent scan", symbol)
                        return
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

    @staticmethod
    def _sector_exposure(
        positions: dict[str, dict],
        incoming_symbol: str,
        account_value: float = 0.0,
    ) -> tuple[dict[str, float], str | None]:
        """Approximate sector exposure using margin-adjusted notional (consistent with exposure gates)."""
        try:
            from yukti.services.universe_scanner_service import SECTOR_STOCKS
        except Exception:
            return {}, None

        denom = account_value or settings.account_value
        symbol_to_sector = {
            symbol: sector
            for sector, members in SECTOR_STOCKS.items()
            for symbol in members
        }
        exposure: dict[str, float] = {}
        for pos_symbol, pos in positions.items():
            sector = symbol_to_sector.get(pos_symbol)
            if not sector:
                continue
            try:
                p_leverage = (
                    settings.intraday_leverage
                    if pos.get("holding_period") == "intraday"
                    else 1.0
                )
                notional = float(pos.get("entry_price", 0)) * int(pos.get("quantity", 0))
                margin = notional / p_leverage
            except Exception:
                continue
            pct = margin / denom * 100 if denom else 0.0
            exposure[sector] = exposure.get(sector, 0.0) + pct

        return exposure, symbol_to_sector.get(incoming_symbol)
