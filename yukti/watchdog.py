"""
yukti/watchdog.py
Dead man's switch — detects when the signal loop stops running.

Problem: if the agent process is alive but the signal loop has deadlocked
or entered an infinite retry, nothing notices. The agent is silently dead.

Solution: signal loop updates a heartbeat timestamp every cycle. A watchdog
task checks it every 60s. If no heartbeat for > 3 * candle_interval_seconds,
alert via Telegram and optionally auto-halt.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from yukti.config import settings

log = logging.getLogger(__name__)

# ── Module-level heartbeat timestamp ──────────────────────────────────────────
_last_heartbeat: float = time.time()
_last_alert_sent: Optional[float] = None


def heartbeat() -> None:
    """Called at the end of every signal loop cycle to mark the loop alive."""
    global _last_heartbeat
    _last_heartbeat = time.time()


def seconds_since_heartbeat() -> float:
    return time.time() - _last_heartbeat


# ── Watchdog task ─────────────────────────────────────────────────────────────

async def watchdog_loop(
    check_interval: int     = 60,
    timeout_multiplier: int = 3,
    auto_halt: bool         = True,
) -> None:
    """
    Runs in the background. Checks heartbeat every `check_interval` seconds.

    Alerts if silence > (timeout_multiplier * candle_interval_seconds).
    Default: candle_interval=5min → alert after 15 minutes of no heartbeat.
    """
    global _last_alert_sent

    candle_secs = int(settings.candle_interval) * 60
    timeout     = candle_secs * timeout_multiplier
    log.info("Watchdog started — alert if no heartbeat for %ds", timeout)

    # Grace period — don't alert immediately on startup
    await asyncio.sleep(timeout)

    while True:
        await asyncio.sleep(check_interval)
        elapsed = seconds_since_heartbeat()

        if elapsed > timeout:
            # Suppress duplicate alerts — at most 1 per 15 min
            now = time.time()
            if _last_alert_sent and (now - _last_alert_sent) < 900:
                continue
            _last_alert_sent = now

            log.critical(
                "WATCHDOG TRIPPED: signal loop silent for %.0fs (threshold %ds)",
                elapsed, timeout,
            )

            try:
                from yukti.telegram.bot import alert
                msg = (
                    f"🚨 *Yukti Watchdog Alert*\n\n"
                    f"Signal loop has not heartbeat for *{elapsed:.0f}s*.\n"
                    f"The agent process is alive but the loop may be deadlocked."
                )
                if auto_halt:
                    from yukti.data.state import set_halt
                    await set_halt(True)
                    msg += "\n\nAgent has been *auto-halted* for safety.\nUse /resume after investigating."
                await alert(msg)
            except Exception as exc:
                log.error("Watchdog alert failed: %s", exc)
