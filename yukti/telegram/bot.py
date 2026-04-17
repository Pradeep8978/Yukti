"""
yukti/telegram/bot.py + commands.py
Telegram bot for Yukti — alerts, kill switch, status commands.
"""
from __future__ import annotations

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
from yukti.execution.dhan_client import dhan

log = logging.getLogger(__name__)

_app: Application | None = None


def get_app() -> Application:
    global _app
    if _app is None:
        _app = Application.builder().token(settings.telegram_bot_token).build()
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
    return str(update.effective_chat.id) == settings.telegram_chat_id


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "Yukti (युक्ति) trading agent active.\n\n"
        "Commands:\n"
        "/status — agent status\n"
        "/pnl — today's P&L\n"
        "/positions — open positions\n"
        "/halt — stop all trading\n"
        "/resume — resume trading\n"
        "/squareoff — close all positions at market"
    )


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
    text = (
        f"*Yukti Status* — {datetime.now().strftime('%H:%M IST')}\n\n"
        f"Agent: {status_icon}\n"
        f"Open positions: {len(positions)}\n"
        f"Today P&L: {perf['daily_pnl_pct']:+.2f}%\n"
        f"Consecutive losses: {perf['consecutive_losses']}\n"
        f"Win rate (last 10): {perf['win_rate_last_10']:.0%}\n"
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
            result = await dhan.market_exit(security_id, direction, qty, product_type)
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
