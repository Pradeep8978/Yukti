"""
yukti/telegram/bot.py + commands.py
Telegram bot for Yukti — alerts, kill switch, status commands.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from yukti.config import settings
from yukti.data.state import (
    get_all_positions,
    get_daily_pnl_pct,
    get_performance_state,
    is_halted,
    set_halt,
)
from yukti.execution.broker_factory import get_broker

log = logging.getLogger(__name__)

_app: Application | None = None


def get_app() -> Application:
    global _app
    if _app is None:
        _app = Application.builder().token(settings.telegram_bot_token).build()
        _app.add_handler(CommandHandler("health",     cmd_health))
        _app.add_handler(CommandHandler("halt",       cmd_halt))
        _app.add_handler(CommandHandler("resume",     cmd_resume))
        _app.add_handler(CommandHandler("status",     cmd_status))
        _app.add_handler(CommandHandler("pnl",        cmd_pnl))
        _app.add_handler(CommandHandler("positions",  cmd_positions))
        _app.add_handler(CommandHandler("squareoff",  cmd_squareoff))
        _app.add_handler(CommandHandler("start",      cmd_start))
    return _app


# ── Auth guard ────────────────────────────────────────────────────────────────

def _authorized(update: Update) -> bool:
    incoming = str(update.effective_chat.id)
    if incoming != settings.telegram_chat_id:
        log.warning(
            "Telegram command from unauthorized chat_id=%s (expected %s) — ignoring",
            incoming, settings.telegram_chat_id or "<unset>",
        )
        return False
    return True


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "Yukti (युक्ति) trading agent active.\n\n"
        "Commands:\n"
        "/health — dependency health check\n"
        "/status — agent status\n"
        "/pnl — today's P&L\n"
        "/positions — open positions\n"
        "/halt — stop all trading\n"
        "/resume — resume trading\n"
        "/squareoff — close all positions at market"
    )


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    halted = await is_halted()
    positions = await get_all_positions()

    db_ok = False
    db_error: str | None = None
    try:
        from sqlalchemy import select
        from yukti.data.database import get_db

        async with get_db() as db:
            await db.execute(select(1))
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        db_error = str(exc)

    redis_ok = False
    redis_error: str | None = None
    try:
        from yukti.data.state import get_redis

        redis_client = await get_redis()
        redis_ok = bool(await redis_client.ping())
    except Exception as exc:  # noqa: BLE001
        redis_error = str(exc)

    def _dhan_check() -> dict[str, Any]:
        import requests

        token = settings.dhan_access_token
        client_id = settings.dhan_client_id
        base = (settings.dhan_base_url or "").rstrip("/")
        if not base:
            return {"ok": False, "error": "no base url"}

        headers: dict[str, str] = {}
        if token:
            headers["access-token"] = token
        if client_id:
            headers["dhanClientId"] = client_id

        try:
            response = requests.get(f"{base}/profile", headers=headers, timeout=5)
            if response.status_code == 200:
                return {"ok": True}
            return {
                "ok": False,
                "status_code": response.status_code,
                "body": response.text[:120],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    dhan_info = await asyncio.to_thread(_dhan_check)

    health_icon = "✅" if db_ok and redis_ok and dhan_info.get("ok", False) and not halted else "⚠️"
    lines = [
        f"{health_icon} *Yukti Health*",
        f"Agent: {'HALTED' if halted else 'ACTIVE'}",
        f"Open positions: {len(positions)}",
        f"Database: {'OK' if db_ok else f'FAIL ({db_error})'}",
        f"Redis: {'OK' if redis_ok else f'FAIL ({redis_error})'}",
        f"Dhan: {'OK' if dhan_info.get('ok', False) else f'FAIL ({dhan_info.get('error') or dhan_info.get('status_code', 'unknown')})'}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_halt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await set_halt(True)
    log.critical("KILL SWITCH activated via Telegram by %s", update.effective_user.username)
    await update.message.reply_text("🛑 HALT activated. No new trades will be placed.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await set_halt(False)
    log.info("Agent resumed via Telegram")
    await update.message.reply_text("✅ Agent resumed. Trading active.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    halted = await is_halted()
    perf   = await get_performance_state()
    positions = await get_all_positions()

    status_icon = "🛑 HALTED" if halted else "✅ ACTIVE"
    wr_count = perf.get("win_rate_last_10_count", 0)
    wr_str = f"{perf['win_rate_last_10']:.0%} ({wr_count}/10)" if wr_count else "— (no trades yet)"
    text = (
        f"*Yukti Status* — {datetime.now().strftime('%H:%M IST')}\n\n"
        f"Agent: {status_icon}\n"
        f"Open positions: {len(positions)}\n"
        f"Today P&L: {perf['daily_pnl_pct']:+.2f}%\n"
        f"Consecutive losses: {perf['consecutive_losses']}\n"
        f"Win rate (last 10): {wr_str}\n"
        f"Trades today: {perf['trades_today']}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    pnl = await get_daily_pnl_pct()
    icon = "✅" if pnl >= 0 else "❌"
    await update.message.reply_text(f"{icon} Today P&L: *{pnl:+.2f}%*", parse_mode="Markdown")


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    positions = await get_all_positions()
    if not positions:
        await update.message.reply_text("No open positions.")
        return

    lines = []
    for sym, pos in positions.items():
        direction = pos.get("direction", "")
        qty       = pos.get("quantity", 0)
        entry     = pos.get("entry_price", 0)
        sl        = pos.get("stop_loss", 0)
        t1        = pos.get("target_1", 0)
        status    = pos.get("status", "")
        icon      = "🟢" if direction == "LONG" else "🔴"
        lines.append(
            f"{icon} *{sym}* {direction} {qty} shares\n"
            f"   Entry ₹{entry:.2f} | SL ₹{sl:.2f} | T1 ₹{t1:.2f} | {status}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def cmd_squareoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await set_halt(True)
    positions = await get_all_positions()
    if not positions:
        await update.message.reply_text("No open positions to square off.")
        return

    await update.message.reply_text(
        f"🛑 Halting + squaring off {len(positions)} position(s)..."
    )

    from yukti.execution.order_sm import close_trade
    for sym, pos in positions.items():
        security_id  = pos.get("security_id", "")
        direction    = pos.get("direction", "LONG")
        qty          = int(pos.get("quantity", 0))
        product_type = "INTRADAY" if pos.get("holding_period") == "intraday" else "DELIVERY"
        try:
            result = await get_broker().market_exit(security_id, direction, qty, product_type)
            exit_p = float(pos.get("entry_price", 0))
            await close_trade(sym, exit_p, "manual_squareoff")
            await update.message.reply_text(f"✅ Closed {sym} {qty} shares")
        except Exception as exc:
            await update.message.reply_text(f"❌ Failed to close {sym}: {exc}")


# ── Alert helpers (called by the agent, not commands) ─────────────────────────

async def alert(text: str) -> None:
    """Send a plain text alert to the configured Telegram chat."""
    try:
        app = get_app()
        await app.bot.send_message(
            chat_id    = settings.telegram_chat_id,
            text       = text,
            parse_mode = "Markdown",
        )
    except Exception as exc:
        log.warning("Telegram alert failed: %s", exc)


async def alert_trade_opened(pos: dict) -> None:
    icon = "🟢 LONG" if pos.get("direction") == "LONG" else "🔴 SHORT"
    await alert(
        f"{icon} *{pos['symbol']}* opened\n"
        f"Entry ₹{pos.get('entry_price', 0):.2f} | "
        f"SL ₹{pos.get('stop_loss', 0):.2f} | "
        f"T1 ₹{pos.get('target_1', 0):.2f}\n"
        f"Qty: {pos.get('quantity', 0)} | Conviction: {pos.get('conviction', 0)}/10"
    )


async def alert_trade_closed(pos: dict) -> None:
    pnl  = float(pos.get("pnl_pct", 0))
    icon = "✅" if pnl > 0 else "❌"
    await alert(
        f"{icon} *{pos['symbol']}* closed: *{pnl:+.2f}%*\n"
        f"Exit ₹{pos.get('exit_price', 0):.2f} | Reason: {pos.get('exit_reason', '')}"
    )


async def alert_agent_started(mode: str) -> None:
    await alert(f"🚀 *Yukti started* in *{mode.upper()}* mode")


async def alert_agent_shutdown(reason: str = "") -> None:
    msg = "🔴 *Yukti stopped*"
    if reason:
        msg += f"\nReason: {reason}"
    await alert(msg)


async def alert_risk_halt(daily_pnl_pct: float, limit_pct: float) -> None:
    await alert(
        f"🛑 *Daily Loss Limit Hit*\n"
        f"P&L today: *{daily_pnl_pct:+.2f}%* (limit: -{limit_pct:.2f}%)\n"
        "All new trades halted for today. Use /resume to manually override."
    )


async def alert_circuit_breaker(nifty_drop_pct: float) -> None:
    await alert(
        f"⛔ *NSE Circuit Breaker Triggered*\n"
        f"Nifty down *{nifty_drop_pct:.2f}%* intraday.\n"
        "All new entries blocked until market recovers above -5%."
    )


async def alert_order_failed(symbol: str, side: str, error: str) -> None:
    await alert(
        f"❌ *Order Failed: {symbol}*\n"
        f"Side: {side}\n"
        f"Error: `{error}`"
    )


async def alert_eod_squareoff_failed(symbol: str, error: str) -> None:
    await alert(
        f"🚨 *EOD Squareoff Failed: {symbol}*\n"
        "Position could not be closed at market. *MANUAL ACTION REQUIRED.*\n"
        f"Error: `{error}`"
    )


async def alert_scan_error(error: str, failed: int = 0, total: int = 0) -> None:
    detail = f" ({failed}/{total} symbols failed)" if failed and total else ""
    await alert(
        f"⚠️ *Scan Cycle Error{detail}*\n"
        f"`{str(error)[:300]}`"
    )


async def alert_job_failed(job_name: str, error: str) -> None:
    await alert(
        f"⚠️ *Scheduled Job Failed: {job_name}*\n"
        f"`{str(error)[:300]}`"
    )
