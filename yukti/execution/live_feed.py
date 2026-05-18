"""
yukti/execution/live_feed.py
WebSocket live feed manager for real-time LTP ticks from DhanHQ.

Runs the DhanHQ feed in a background daemon thread; dispatches incoming
ticks back to the asyncio event loop via asyncio.run_coroutine_threadsafe().

SDK compatibility:
  dhanhq >= 2.0  → MarketFeed(dhan_context, instruments, version='v1')
  dhanhq  < 2.0  → DhanFeed(client_id, access_token, ...)

On WebSocket disconnect the feed marks itself unavailable — the REST
polling loop in monitor.py remains fully functional as a fallback.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Coroutine

log = logging.getLogger(__name__)


class LiveFeedManager:
    """
    Manages the DhanHQ WebSocket live feed for active positions.

    Usage:
        manager = get_feed_manager()
        await manager.start(tick_handler)          # once at startup
        await manager.subscribe(symbol, security_id)
        await manager.unsubscribe(symbol)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._security_to_symbol: dict[str, str] = {}   # security_id → symbol
        self._subscriptions: dict[str, str] = {}         # symbol → security_id
        self._tick_handler: Callable[[str, float], Coroutine] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._feed: Any = None
        self._connected: bool = False

    # ── Public API ───────────────────────────────────────────────

    async def start(self, tick_handler: Callable[[str, float], Coroutine]) -> None:
        """
        Start the WebSocket feed in a daemon thread.
        tick_handler(symbol, ltp) is called for every incoming tick.
        """
        if self._thread and self._thread.is_alive():
            return

        self._tick_handler = tick_handler
        self._loop = asyncio.get_running_loop()

        self._thread = threading.Thread(
            target=self._run_feed, daemon=True, name="dhan-live-feed"
        )
        self._thread.start()
        log.info("LiveFeedManager: WebSocket thread started")

    async def subscribe(self, symbol: str, security_id: str) -> None:
        """Register a symbol for LTP ticks."""
        with self._lock:
            if security_id in self._security_to_symbol:
                return
            self._subscriptions[symbol] = security_id
            self._security_to_symbol[security_id] = symbol
        log.info("LiveFeedManager: subscribed %s (id=%s)", symbol, security_id)

        if self._connected and self._feed is not None:
            if hasattr(self._feed, "subscribe_symbols"):
                try:
                    self._feed.subscribe_symbols([("NSE", security_id, getattr(self._feed, "Ticker", None))])
                except Exception as exc:
                    log.debug("LiveFeedManager: subscribe_symbols failed for %s: %s", symbol, exc)
            else:
                # MarketFeed (dhanhq>=2.0) has no subscribe_symbols — the instrument
                # list is fixed at construction time. Trigger a reconnect so the new
                # symbol is included in the next feed instance's instrument list.
                log.info("LiveFeedManager: %s added — triggering feed reconnect to include new instrument", symbol)
                if self._feed is not None:
                    try:
                        self._feed.disconnect()
                    except Exception:
                        pass

    async def unsubscribe(self, symbol: str) -> None:
        """Remove a symbol from LTP ticks."""
        with self._lock:
            security_id = self._subscriptions.pop(symbol, None)
            if security_id:
                self._security_to_symbol.pop(security_id, None)
        if not security_id:
            return
        log.info("LiveFeedManager: unsubscribed %s", symbol)

        if self._connected and self._feed is not None:
            try:
                self._feed.unsubscribe_symbols([("NSE", security_id, getattr(self._feed, "Ticker", None))])
            except Exception as exc:
                log.debug("LiveFeedManager: unsubscribe_symbols failed for %s: %s", symbol, exc)

    def is_connected(self) -> bool:
        return self._connected

    # ── Internal ─────────────────────────────────────────────────

    def _run_feed(self) -> None:
        """Blocking run in daemon thread with auto-reconnect and exponential backoff.

        Reconnects up to MAX_RETRIES_PER_SESSION times within a trading day before
        giving up. Backoff resets on each clean connect so transient glitches don't
        permanently disable the feed.
        """
        import time
        from datetime import date

        MAX_RETRIES_PER_SESSION = 10
        retry_secs    = 5
        retries_today = 0
        last_date     = date.today()

        while True:
            today = date.today()
            if today != last_date:
                # New calendar day — reset counters
                retries_today = 0
                last_date     = today
                retry_secs    = 5

            if retries_today >= MAX_RETRIES_PER_SESSION:
                log.warning(
                    "LiveFeedManager: %d reconnect attempts today — feed suspended. "
                    "REST polling remains active as fallback.",
                    retries_today,
                )
                # Sleep until next calendar day
                import datetime as _dt
                now       = _dt.datetime.now()
                tomorrow  = (now + _dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
                time.sleep((tomorrow - now).total_seconds())
                continue

            try:
                from yukti.config import settings
                from yukti.execution.broker_factory import get_broker

                broker       = get_broker()
                dhan_client  = getattr(broker, "_dhan", None)
                client_id    = getattr(dhan_client, "client_id",    None) or settings.dhan_client_id
                access_token = getattr(dhan_client, "access_token", None) or settings.dhan_access_token

                if not client_id or not access_token:
                    log.warning("LiveFeedManager: missing client_id/access_token — feed disabled")
                    return

                # Rebuild instrument list from current subscriptions each attempt
                # so positions added after the last connect are included.
                with self._lock:
                    pre_instruments = [
                        {"ExchangeSegment": "NSE_EQ", "SecurityId": sid}
                        for sid in self._subscriptions.values()
                    ]

                # Don't burn retry attempts with an empty subscription list —
                # MarketFeed.run_forever() returns instantly when there's
                # nothing to subscribe to, which would exhaust the daily 10
                # before market open. Idle-wait until a position subscribes.
                if not pre_instruments:
                    log.debug("LiveFeedManager: no subscriptions yet — idle wait 10s")
                    time.sleep(10)
                    continue

                self._feed = self._create_feed(broker, client_id, access_token, pre_instruments)
                if self._feed is None:
                    # Permanent failure (missing dhanhq package etc.) — don't loop
                    return

                self._connected = True
                retry_secs    = 5   # reset backoff on successful connect
                retries_today += 1
                log.info(
                    "LiveFeedManager: starting run_forever (LTP mode, attempt %d/%d today, "
                    "subs=%d)",
                    retries_today, MAX_RETRIES_PER_SESSION, len(pre_instruments),
                )
                t_start = time.time()
                self._feed.run_forever()   # blocks until WebSocket closes
                duration = time.time() - t_start
                log.warning(
                    "LiveFeedManager: run_forever returned after %.1fs — connection "
                    "closed (likely market closed or auth issue; see prior _on_error/_on_close)",
                    duration,
                )

            except Exception as exc:
                log.warning("LiveFeedManager: feed thread error: %s", exc, exc_info=True)
            finally:
                self._connected = False

            log.info("LiveFeedManager: reconnecting in %ds ...", retry_secs)
            time.sleep(retry_secs)
            retry_secs = min(retry_secs * 2, 60)   # cap at 60s

    def _create_feed(
        self,
        broker: Any,
        client_id: str,
        access_token: str,
        pre_instruments: list[dict],
    ) -> Any:
        """Try MarketFeed (dhanhq>=2.0) then DhanFeed (<2.0). Returns feed or None."""
        # dhanhq >= 2.0: MarketFeed(dhan_context, instruments, version)
        try:
            from dhanhq import MarketFeed  # type: ignore[attr-defined]

            dhan_context = getattr(broker, "_ctx", None)
            if dhan_context is None:
                raise TypeError("DhanContext unavailable on broker")

            instruments = [
                (MarketFeed.NSE, i["SecurityId"], MarketFeed.Ticker)
                for i in pre_instruments
            ] if pre_instruments else []

            feed = MarketFeed(
                dhan_context,
                instruments,
                version = "v2",
                on_message = self._on_message,
                on_close   = self._on_close,
                on_error   = self._on_error,
            )
            log.info("LiveFeedManager: using MarketFeed (dhanhq>=2.0)")
            return feed
        except (ImportError, AttributeError, TypeError):
            pass

        # dhanhq < 2.0: DhanFeed(client_id, access_token, ...)
        try:
            from dhanhq import DhanFeed  # type: ignore[attr-defined]

            feed = DhanFeed(
                client_id         = client_id,
                access_token      = access_token,
                subscription_code = DhanFeed.LTP,
                on_message        = self._on_message,
                on_close          = self._on_close,
                on_error          = self._on_error,
            )
            if pre_instruments:
                try:
                    feed.subscribe_symbols(pre_instruments)
                except Exception as exc:
                    log.debug("LiveFeedManager: pre-connect subscription failed: %s", exc)
            log.info("LiveFeedManager: using DhanFeed (dhanhq<2.0)")
            return feed
        except (ImportError, AttributeError, TypeError):
            pass

        log.warning("LiveFeedManager: dhanhq package missing or unsupported version — feed disabled")
        return None

    def _on_message(self, *args: Any) -> None:
        """Dispatch incoming tick to asyncio tick handler.

        DhanHQ payload field names vary between SDK versions and API v1/v2.
        We try every known variant before giving up so a schema change doesn't
        silently drop ticks.
        """
        try:
            # SDK passes either on_message(ws, data) or on_message(data)
            data = None
            for arg in reversed(args):
                if isinstance(arg, dict):
                    data = arg
                    break
            if data is None:
                return

            security_id = str(
                data.get("security_id")
                or data.get("securityId")
                or data.get("SecurityId")
                or data.get("sym")
                or ""
            )
            # LTP field name differs between v1 (LTP) and v2 (last_price / ltp)
            ltp_raw = (
                data.get("LTP")
                or data.get("last_price")
                or data.get("lastPrice")
                or data.get("ltp")
            )
            if not security_id or ltp_raw is None:
                log.debug("LiveFeedManager: unrecognised tick payload — keys: %s", list(data.keys()))
                return
            ltp = float(ltp_raw)
            if ltp <= 0:
                return

            with self._lock:
                symbol = self._security_to_symbol.get(security_id)
            if not symbol:
                return

            if self._tick_handler and self._loop and not self._loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._tick_handler(symbol, ltp),
                    self._loop,
                )
        except Exception as exc:
            log.debug("LiveFeedManager: _on_message error: %s", exc)

    def _on_close(self, *args: Any) -> None:
        self._connected = False
        # Include any args the SDK passes (status code, reason) so we can see
        # *why* the connection closed (e.g., 1006 abnormal, 1011 server error).
        log.warning("LiveFeedManager: WebSocket disconnected args=%r — falling back to REST polling", args)

    def _on_error(self, *args: Any) -> None:
        # WARNING (was DEBUG) — without this the reason for repeated reconnect
        # cycles is invisible. Critical for diagnosing market-hours feed loss.
        # *args because the SDK calls this as (ws, error) on some versions and
        # (error,) on others; the previous fixed signature crashed run_forever.
        log.warning("LiveFeedManager: WebSocket error: %r", args)


# Module-level singleton
_feed_manager = LiveFeedManager()


def get_feed_manager() -> LiveFeedManager:
    return _feed_manager
