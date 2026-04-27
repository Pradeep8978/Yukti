"""
yukti/scheduler/jobs.py
APScheduler cron jobs and NSE trading calendar.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger(__name__)
from yukti.config import settings
from yukti.jobs.meta_lessons import generate_meta_lessons

# Timezone for NSE (India Standard Time)
KOLKATA = ZoneInfo("Asia/Kolkata")

# ── NSE holidays (update annually from NSE circular) ─────────────────────────
NSE_HOLIDAYS: set[date] = {
    date(2025, 1, 26), date(2025, 3, 14), date(2025, 4, 14),
    date(2025, 4, 18), date(2025, 5, 1),  date(2025, 8, 15),
    date(2025, 10, 2), date(2025, 10, 24), date(2025, 11, 5),
    date(2025, 12, 25),
    date(2026, 1, 26), date(2026, 8, 15), date(2026, 10, 2),
    date(2026, 12, 25),
}


def is_trading_day(d: date | None = None) -> bool:
    # Evaluate trading day in Asia/Kolkata timezone to avoid host-UTC mismatches
    if d is None:
        d = datetime.now(KOLKATA).date()
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def is_trading_hours(now: datetime | None = None) -> bool:
    # Compare times in Asia/Kolkata timezone
    if now is None:
        now = datetime.now(KOLKATA)
    else:
        # If naive datetime provided, treat it as UTC and convert to IST
        if now.tzinfo is None:
            now = datetime.fromtimestamp(now.timestamp(), tz=KOLKATA)
        else:
            now = now.astimezone(KOLKATA)
    t = now.time()
    return time(9, 15) <= t <= time(15, 10)


def is_fo_expiry(d: date | None = None) -> bool:
    if d is None:
        d = datetime.now(KOLKATA).date()
    if d.weekday() != 3:
        return False
    return (d + timedelta(days=7)).month != d.month


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def job_morning_prep() -> None:
    log.info("=== morning prep ===")
    from yukti.execution.reconcile import reconcile_positions
    await reconcile_positions()


async def job_eod_squareoff() -> None:
    log.info("=== EOD squareoff ===")
    from yukti.data.state import get_all_positions
    from yukti.execution.dhan_client import dhan
    from yukti.execution.order_sm import close_trade

    for symbol, pos in (await get_all_positions()).items():
        if pos.get("holding_period") != "intraday":
            continue
        if pos.get("status") not in ("ARMED", "FILLED"):
            continue
        sec  = pos.get("security_id", "")
        qty  = int(pos.get("quantity", 0))
        dirn = pos.get("direction", "LONG")
        try:
            for gtt in [pos.get("sl_gtt_id"), pos.get("target_gtt_id")]:
                if gtt:
                    await dhan.cancel_gtt(gtt)
            await dhan.market_exit(sec, dirn, qty, "INTRADAY")
            await close_trade(symbol, float(pos.get("entry_price", 0)), "eod_squareoff")
            log.info("EOD closed %s", symbol)
        except Exception as exc:
            log.error("EOD squareoff failed %s: %s", symbol, exc)


async def job_daily_reset() -> None:
    log.info("=== daily reset ===")
    from yukti.data.state import reset_daily_pnl, reset_trades_today
    await reset_daily_pnl()
    await reset_trades_today()
    log.info("Daily counters reset")
    log.info("=== daily journal ===")
    from datetime import date as dt_date
    from yukti.data.database import get_db
    from yukti.data.models import Trade
    from yukti.agents.journal import write_journal_entry
    from yukti.agents.memory import store_journal
    from sqlalchemy import select, func as sa_func

    today = dt_date.today()
    async with get_db() as db:
        rows = (await db.execute(
            select(Trade).where(
                sa_func.date(Trade.closed_at) == today,
                Trade.pnl.is_not(None),
            )
        )).scalars().all()

    for t in rows:
        try:
            # Get market bias from trade reasoning or default to NEUTRAL
            market_bias = "NEUTRAL"
            if t.reasoning:
                if "BULLISH" in t.reasoning.upper():
                    market_bias = "BULLISH"
                elif "BEARISH" in t.reasoning.upper():
                    market_bias = "BEARISH"
            
            refl = await write_journal_entry(
                symbol=t.symbol, direction=t.direction, setup_type=t.setup_type,
                entry=t.entry_price, stop_loss=t.stop_loss, target=t.target_1,
                exit_price=t.exit_price or t.entry_price,
                exit_reason=t.exit_reason or "", pnl_pct=t.pnl_pct or 0.0,
                conviction=t.conviction, reasoning=t.reasoning,
                market_bias=market_bias,
            )
            await store_journal(
                t.id, t.symbol, t.setup_type, t.direction,
                t.pnl_pct or 0.0, refl,
                conviction=t.conviction,
            )
        except Exception as exc:
            log.error("Journal failed trade %d: %s", t.id, exc)


async def job_learning_loop() -> None:
    """Embed journal entries and write vectors to Postgres (runs at low-traffic hour)."""
    if not getattr(settings, "voyage_api_key", None):
        log.info("LearningLoop job skipped: voyage API key not configured")
        return
    log.info("=== learning loop: embedding pending journals ===")
    from yukti.services.learning_loop_service import LearningLoopService
    svc = LearningLoopService()
    try:
        count = await svc.run_once()
        log.info("LearningLoop: processed %d entries", count)
    except Exception as exc:
        log.error("LearningLoop job failed: %s", exc)


async def job_daily_report() -> None:
    from yukti.data.state import get_performance_state
    from yukti.telegram.bot import alert
    perf = await get_performance_state()
    icon = "✅" if perf["daily_pnl_pct"] >= 0 else "❌"
    await alert(
        f"{icon} *Yukti Daily Summary*\n"
        f"P&L: {perf['daily_pnl_pct']:+.2f}% | Trades: {perf['trades_today']}\n"
        f"Win rate (L10): {perf['win_rate_last_10']:.0%} | "
        f"Streak losses: {perf['consecutive_losses']}"
    )


async def job_meta_lessons() -> None:
    """Daily EOD job: aggregate top key_lessons and write data/meta_lessons.json.

    Gated by `enable_meta_lessons` in Settings (default True).
    Only runs on NSE trading days.
    """
    if not is_trading_day():
        log.info("Meta-lessons job skipped: non-trading day")
        return
    log.info("=== meta-lessons aggregation ===")
    try:
        payload = await generate_meta_lessons()
        log.info("Meta-lessons done: %d lessons written", len(payload.get("lessons", [])))
    except Exception as exc:
        log.error("Meta-lessons job failed: %s", exc)


async def job_universe_scan() -> None:
    """Pre-market universe scan at 08:45 IST."""
    log.info("=== universe scan (primary) ===")
    from yukti.services.universe_scanner_service import UniverseScannerService
    scanner = UniverseScannerService()
    await scanner.run_with_fallback(is_refresh=False)


async def job_universe_refresh() -> None:
    """Intraday universe refresh — add new movers, never remove."""
    log.info("=== universe refresh ===")
    from yukti.services.universe_scanner_service import UniverseScannerService
    scanner = UniverseScannerService()
    await scanner.run_with_fallback(is_refresh=True)


def build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="Asia/Kolkata")
    sched.add_job(job_universe_scan,    "cron", hour=8,  minute=45)
    sched.add_job(job_morning_prep,     "cron", hour=9,  minute=0)
    sched.add_job(job_universe_refresh, "cron", hour=10, minute=0)
    sched.add_job(job_universe_refresh, "cron", hour=12, minute=0)
    sched.add_job(job_eod_squareoff,    "cron", hour=15, minute=10)
    sched.add_job(job_daily_reset,      "cron", hour=16, minute=0)
    sched.add_job(job_daily_report,     "cron", hour=16, minute=30)

    # Learning loop: embed pending journals (config-gated, runs at 2am)
    if getattr(settings, "enable_learning_loop", False):
        sched.add_job(job_learning_loop, "cron", hour=2, minute=0)

    # Self-learning loop: export→retrain→eval→promote (config-gated, runs at 3am)
    if getattr(settings, "enable_self_learning", True):
        sched.add_job(job_self_learning_loop, "cron", hour=3, minute=0)

    # Meta-lessons: aggregate key_lessons → data/meta_lessons.json (config-gated)
    # Runs daily at `daily_journal_summary_time` (default: 16:05 IST, 5m after journals)
    if settings.enable_meta_lessons:
        tj = settings.daily_journal_summary_time or "16:05"
        try:
            hh, mm = (int(p) for p in tj.split(":"))
        except Exception:
            hh, mm = 16, 5
        sched.add_job(job_meta_lessons, "cron", hour=hh, minute=mm,
                      id="meta_lessons", replace_existing=True)
        log.info("Meta-lessons job scheduled at %02d:%02d IST", hh, mm)

    return sched


# ── Self-learning loop: continuous ingestion, retrain, eval, promote ──────────

import json

import redis.asyncio as aioredis

async def job_self_learning_loop() -> None:
    """Self-learning loop: export new data, retrain if enough, evaluate, promote if pass."""
    import torch
    import os
    from datetime import datetime, timedelta
    import asyncio
    log = logging.getLogger("self_learning_loop")
    from yukti.config import settings
    # Locking: try Redis, fallback to file lock
    lock_key = "yukti:self_learning_lock"
    lock_ttl = 60 * 60  # 1 hour
    redis_url = getattr(settings, "redis_url", "redis://localhost:6379/0")
    r = None
    have_lock = False
    file_lock_path = "data/training/self_learning.lock"
    using_file_lock = False
    try:
        try:
            r = await aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
            have_lock = await r.set(lock_key, "1", ex=lock_ttl, nx=True)
            if not have_lock:
                log.warning("Self-learning loop already running (Redis lock held)")
                return
        except Exception as exc:
            log.warning(f"Redis unavailable ({exc}); falling back to file lock")
            # File lock fallback
            import os, time
            if os.path.exists(file_lock_path):
                # If lock file is recent (< lock_ttl), abort
                if time.time() - os.path.getmtime(file_lock_path) < lock_ttl:
                    log.warning("Self-learning loop already running (file lock held)")
                    return
                else:
                    os.remove(file_lock_path)
            with open(file_lock_path, "w") as fh:
                fh.write(str(os.getpid()))
            have_lock = True
            using_file_lock = True

        # 1. Export new data since last run (default: last 7 days)
        out_path = "data/training/journal_export.jsonl"
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        log.info(f"[SelfLearning] Exporting new data since {since}")
        artifact_dir = f"artifacts/self_learning/{datetime.now().strftime('%Y%m%d')}"
        os.makedirs(artifact_dir, exist_ok=True)
        exporter_log = os.path.join(artifact_dir, "exporter.log")
        proc = await asyncio.create_subprocess_exec(
            "python", "scripts/export_training_data.py", "--out", out_path, "--since", since,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        with open(exporter_log, "w", encoding="utf-8") as fh:
            fh.write(stdout.decode(errors="replace"))
            fh.write("\n--- STDERR ---\n")
            fh.write(stderr.decode(errors="replace"))
        if proc.returncode != 0:
            log.error(f"Exporter failed: {stderr.decode().strip()} (see {exporter_log})")
            return
        log.info(f"Exporter output: {stdout.decode().strip()} (full log: {exporter_log})")

        # 2. Check if enough new data (configurable)
        min_rows = getattr(settings, "self_learning_min_rows", 100)
        n_rows = 0
        try:
            with open(out_path, "r", encoding="utf-8") as fh:
                n_rows = sum(1 for _ in fh)
        except Exception as exc:
            log.error(f"Failed to count exported rows: {exc}")
            return
        if n_rows < min_rows:
            log.info(f"Not enough new data for retraining: {n_rows} rows (min required: {min_rows})")
            return
        log.info(f"Proceeding to retrain: {n_rows} rows")

        # 3. Retrain adapter (dry-run if no GPU)
        model_id = "facebook/opt-125m"
        out_dir = f"models/lora-auto-{datetime.now().strftime('%Y%m%d')}"
        dry_run = not torch.cuda.is_available()
        log.info(f"Training adapter to {out_dir} (dry_run={dry_run})")
        train_args = [
            "python", "trainer/train_adapter.py", "--data", out_path, "--model", model_id, "--out_dir", out_dir, "--epochs", "3", "--use_peft", "auto"
        ]
        if dry_run:
            train_args.append("--dry_run")
        trainer_log = os.path.join(artifact_dir, "trainer.log")
        proc = await asyncio.create_subprocess_exec(
            *train_args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        with open(trainer_log, "w", encoding="utf-8") as fh:
            fh.write(stdout.decode(errors="replace"))
            fh.write("\n--- STDERR ---\n")
            fh.write(stderr.decode(errors="replace"))
        if proc.returncode != 0:
            log.error(f"Trainer failed: {stderr.decode().strip()} (see {trainer_log})")
            return
        log.info(f"Trainer output: {stdout.decode().strip()} (full log: {trainer_log})")

        # 4. Evaluate new adapter vs baseline
        eval_dir = f"artifacts/eval/{datetime.now().strftime('%Y%m%d')}"
        os.makedirs(eval_dir, exist_ok=True)
        log.info(f"Evaluating new adapter in {eval_dir}")
        eval_args = [
            "python", "trainer/evaluate_vs_baseline.py", "--adapter_dir", out_dir, "--base_model", model_id, "--out_dir", eval_dir
        ]
        eval_log = os.path.join(artifact_dir, "evaluate.log")
        proc = await asyncio.create_subprocess_exec(
            *eval_args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        with open(eval_log, "w", encoding="utf-8") as fh:
            fh.write(stdout.decode(errors="replace"))
            fh.write("\n--- STDERR ---\n")
            fh.write(stderr.decode(errors="replace"))
        if proc.returncode != 0:
            log.error(f"Evaluation failed: {stderr.decode().strip()} (see {eval_log})")
            return
        log.info(f"Evaluation output: {stdout.decode().strip()} (full log: {eval_log})")

        # 5. Check metrics and promote if pass (configurable thresholds).
        # If promoted, package artifacts, optionally upload to S3, set canary pointer
        # and run a short monitoring evaluation. Roll back automatically on failure.
        try:
            from yukti.artifacts import package_and_publish
            from yukti.agents import canary as canary_mod
            from yukti.metrics import (
                self_learning_runs, self_learning_failures,
                canary_promotions, canary_rollbacks,
            )
        except Exception:
            canary_mod = None

        # Count this run
        try:
            from yukti.metrics import self_learning_runs
            self_learning_runs.inc()
        except Exception:
            pass

        metrics_path = os.path.join(eval_dir, "compare_metrics.json")
        try:
            with open(metrics_path, "r", encoding="utf-8") as fh:
                metrics = json.load(fh)
            candidate = metrics.get("candidate") or {}
            thresholds = getattr(settings, "self_learning_thresholds", {"win_rate": 0.55, "profit_factor": 1.2})
            win_rate = float(candidate.get("win_rate", 0.0))
            profit_factor = float(candidate.get("profit_factor", 0.0))

            passed = (win_rate > thresholds.get("win_rate", 0.55) and
                      profit_factor > thresholds.get("profit_factor", 1.2))

            if passed:
                log.info(f"Candidate PASSED: win_rate={win_rate:.2f}, profit_factor={profit_factor:.2f} — PROMOTE to canary")

                # Package & publish artifact (zip + sha + optional S3 upload)
                try:
                    meta = package_and_publish(out_dir, out_dir=artifact_dir)
                    log.info(f"Packaged model artifact: {meta.get('archive_path')} sha={meta.get('sha256')}")
                except Exception as exc:
                    log.warning(f"Artifact packaging failed: {exc}")

                # Promote to canary: record previous value and set active pointer via helper
                try:
                    import shutil
                    canary_dir = f"models/canary/{datetime.now().strftime('%Y%m%d')}"
                    shutil.rmtree(canary_dir, ignore_errors=True)
                    shutil.copytree(out_dir, canary_dir)
                    # use canary helper to set active pointer
                    try:
                        if canary_mod is not None:
                            await canary_mod.set_active_canary(canary_dir)
                    except Exception:
                        # best effort
                        try:
                            with open("models/canary/active.txt", "w", encoding="utf-8") as fh:
                                fh.write(canary_dir)
                        except Exception:
                            pass

                    log.info(f"Promoted candidate to canary: {canary_dir}")
                    try:
                        from yukti.metrics import canary_promotions
                        canary_promotions.inc()
                    except Exception:
                        pass
                except Exception as exc:
                    log.error(f"Canary promotion failed: {exc}")

                # Telegram alert for promotion
                try:
                    from yukti.telegram.bot import alert as tg_alert
                    import asyncio as _asyncio
                    msg = (
                        f"🚀 *Self-learning promotion: CANARY DEPLOYED*\n"
                        f"Model: `{canary_dir}`\n"
                        f"Win rate: {win_rate:.2%}\nProfit factor: {profit_factor:.2f}"
                    )
                    _asyncio.create_task(tg_alert(msg))
                except Exception:
                    pass

                # Short monitoring evaluation: run a smaller evaluation and rollback on failure
                try:
                    monitor_dir = os.path.join(artifact_dir, "monitor")
                    os.makedirs(monitor_dir, exist_ok=True)
                    mon_args = [
                        "python", "trainer/evaluate_vs_baseline.py",
                        "--adapter_dir", out_dir,
                        "--base_model", model_id,
                        "--out_dir", monitor_dir,
                        "--bootstrap", "100",
                    ]
                    mon_log = os.path.join(artifact_dir, "monitor.log")
                    proc = await asyncio.create_subprocess_exec(
                        *mon_args,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await proc.communicate()
                    with open(mon_log, "w", encoding="utf-8") as fh:
                        fh.write(stdout.decode(errors="replace"))
                        fh.write("\n--- STDERR ---\n")
                        fh.write(stderr.decode(errors="replace"))
                    if proc.returncode != 0:
                        log.warning(f"Monitor evaluation failed: {stderr.decode().strip()} (see {mon_log})")
                    else:
                        # Read monitor metrics
                        try:
                            mon_metrics_path = os.path.join(monitor_dir, "compare_metrics.json")
                            with open(mon_metrics_path, "r", encoding="utf-8") as fh:
                                mon_metrics = json.load(fh)
                            mon_candidate = mon_metrics.get("candidate", {})
                            mon_win = float(mon_candidate.get("win_rate", 0.0))
                            mon_pf = float(mon_candidate.get("profit_factor", 0.0))
                            # If monitor shows regression beyond threshold, rollback
                            if mon_win < thresholds.get("win_rate", 0.55) or mon_pf < thresholds.get("profit_factor", 1.2):
                                # rollback to previous
                                prev = None
                                try:
                                    if canary_mod is not None:
                                        prev = await canary_mod.get_previous_active()
                                except Exception:
                                    prev = None
                                if prev:
                                    try:
                                        if canary_mod is not None:
                                            await canary_mod.set_active_canary(prev)
                                        else:
                                            with open("models/canary/active.txt", "w", encoding="utf-8") as fh:
                                                fh.write(prev)
                                        from yukti.metrics import canary_rollbacks
                                        canary_rollbacks.inc()
                                    except Exception:
                                        log.warning("Rollback attempted but failed to restore previous active canary")
                                # Alert
                                try:
                                    from yukti.telegram.bot import alert as tg_alert
                                    import asyncio as _asyncio
                                    _asyncio.create_task(tg_alert(f"⚠️ Canary rollback triggered after monitoring: win={mon_win:.2%} pf={mon_pf:.2f}"))
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception as exc:
                    log.warning(f"Canary monitor failed (non-fatal): {exc}")

            else:
                log.info(f"Candidate did NOT pass: win_rate={win_rate:.2f}, profit_factor={profit_factor:.2f}")
                try:
                    from yukti.telegram.bot import alert as tg_alert
                    import asyncio as _asyncio
                    msg = (
                        f"❌ *Self-learning candidate rejected*\n"
                        f"Win rate: {win_rate:.2%}\nProfit factor: {profit_factor:.2f}"
                    )
                    _asyncio.create_task(tg_alert(msg))
                except Exception:
                    pass
        except Exception as exc:
            log.error(f"Failed to process promotion/monitoring: {exc}")
            try:
                from yukti.metrics import self_learning_failures
                self_learning_failures.inc()
            except Exception:
                pass
    except Exception as exc:
        log.error(f"Self-learning loop failed: {exc}")
    finally:
        # Release lock
        try:
            if have_lock:
                if using_file_lock:
                    import os
                    if os.path.exists(file_lock_path):
                        os.remove(file_lock_path)
                elif r is not None:
                    await r.delete(lock_key)
        except Exception:
            pass
        # Close Redis connection if open
        try:
            if r is not None:
                await r.close()
                if hasattr(r, "wait_closed"):
                    await r.wait_closed()
        except Exception:
            pass
