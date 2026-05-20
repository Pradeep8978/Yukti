"""
Standalone DhanHQ MarketFeed connection probe.

Verifies WebSocket auth + connectivity end-to-end without touching the
running bot. Burns one of the daily socket-attempt budget — DhanHQ
typically allows 10 connections per access-token per session.

Run inside the yukti container:
    docker exec yukti-yukti-1 /app/.venv/bin/python3 /app/scratch/probe_socket.py

Outcomes:
  PASS  feed_opened=True, immediate_close=False — auth + connectivity OK
        (ticks may or may not arrive depending on market hours)
  FAIL  feed_opened=False OR immediate_close=True — auth/network problem
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from yukti.config import settings

SYMBOL_NAME    = "RELIANCE"
SECURITY_ID    = "2885"
PROBE_SECS     = 15
IMMEDIATE_CLOSE_SECS = 2.0   # run_forever returning faster than this == auth/connect failure


def main() -> int:
    print(f"[probe] start at {datetime.utcnow().isoformat()}Z")
    print(f"[probe] client_id={settings.dhan_client_id} token_len={len(settings.dhan_access_token)}")

    try:
        from dhanhq import DhanContext, MarketFeed  # type: ignore[attr-defined]
    except Exception as exc:
        print(f"[probe] FAIL: dhanhq import error: {exc}")
        return 2

    state = {
        "messages":        0,
        "first_message":   None,
        "errors":          [],
        "closes":          [],
        "feed_constructed": False,
    }

    def on_message(*args):
        state["messages"] += 1
        if state["first_message"] is None:
            state["first_message"] = time.time()
            sample = args[-1] if args else None
            print(f"[probe] tick received: {str(sample)[:200]}")

    def on_error(*args):
        msg = " ".join(str(a) for a in args)
        state["errors"].append(msg)
        print(f"[probe] on_error: {msg[:300]}")

    def on_close(*args):
        msg = " ".join(str(a) for a in args)
        state["closes"].append(msg)
        print(f"[probe] on_close: {msg[:300]}")

    try:
        ctx = DhanContext(client_id=settings.dhan_client_id,
                          access_token=settings.dhan_access_token)
        instruments = [(MarketFeed.NSE, SECURITY_ID, MarketFeed.Ticker)]
        feed = MarketFeed(
            ctx,
            instruments,
            version    = "v2",
            on_message = on_message,
            on_error   = on_error,
            on_close   = on_close,
        )
        state["feed_constructed"] = True
        print(f"[probe] MarketFeed constructed; instruments={instruments}")
    except Exception as exc:
        print(f"[probe] FAIL: MarketFeed construction error: {exc}")
        return 3

    t_start = time.time()
    def runner():
        try:
            feed.run_forever()
        except Exception as exc:
            state["errors"].append(f"run_forever exception: {exc}")
            print(f"[probe] run_forever exception: {exc}")

    th = threading.Thread(target=runner, name="probe-feed", daemon=True)
    th.start()
    print(f"[probe] run_forever started, sampling for {PROBE_SECS}s")
    time.sleep(PROBE_SECS)

    elapsed = time.time() - t_start

    try:
        feed.disconnect()
    except Exception as exc:
        print(f"[probe] disconnect (best-effort) failed: {exc}")

    th.join(timeout=3)
    immediate_close = (not th.is_alive()) and elapsed < IMMEDIATE_CLOSE_SECS

    print()
    print("=== PROBE RESULT ===")
    print(f"feed_constructed : {state['feed_constructed']}")
    print(f"thread_alive_end : {th.is_alive()}")
    print(f"elapsed_seconds  : {elapsed:.1f}")
    print(f"immediate_close  : {immediate_close}")
    print(f"messages         : {state['messages']}")
    print(f"errors           : {len(state['errors'])}")
    print(f"closes           : {len(state['closes'])}")
    if state["errors"]:
        print(f"first error      : {state['errors'][0][:300]}")
    if state["closes"]:
        print(f"first close      : {state['closes'][0][:300]}")

    if not state["feed_constructed"]:
        print("VERDICT: FAIL — could not construct MarketFeed")
        return 4
    if immediate_close:
        print("VERDICT: FAIL — socket closed immediately (auth or whitelist issue)")
        return 5
    if state["errors"]:
        print("VERDICT: PARTIAL — feed errored but didn't close fast; review error above")
        return 1
    print("VERDICT: PASS — socket opened, no immediate close, auth path appears healthy")
    if state["messages"] == 0:
        print("note: 0 messages — expected outside market hours; tick path will be re-verified tomorrow")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
