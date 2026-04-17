"""
scripts/crash_alert.py
Supervisor event listener — sends a Telegram alert when the yukti process crashes.
Configured as an [eventlistener] in supervisor.conf.
"""
from __future__ import annotations

import os
import sys
import httpx


def send_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def main() -> None:
    while True:
        # Supervisor protocol: read header then data
        line = sys.stdin.readline()
        if not line:
            break
        headers = dict(
            pair.split(":", 1)
            for pair in line.strip().split(" ")
            if ":" in pair
        )
        data_len = int(headers.get("len", 0))
        data     = sys.stdin.read(data_len) if data_len else ""

        send_telegram(
            f"🚨 *Yukti CRASHED*\n\n"
            f"Process entered FATAL state.\n"
            f"Supervisor will attempt restart.\n\n"
            f"`{data[:300]}`"
        )

        # Acknowledge event
        sys.stdout.write("RESULT 2\nOK")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
