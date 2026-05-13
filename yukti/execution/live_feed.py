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
            try:
                self._feed.subscribe_symbols([("NSE", security_id, getattr(self._feed, "Ticker", None))])
            except Exception as exc:
                log.debug("LiveFeedManager: subscribe_symbols failed for %s: %s", symbol, exc)

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
        """Blocking run in daemon thread — creates the feed and calls run_forever()."""
        try:
            from yukti.config import settings
            from yukti.execution.broker_factory import get_broker

            broker = get_broker()
            dhan_client = getattr(broker, "_dhan", None)

            client_id    = getattr(dhan_client, "client_id",    None) or getattr(settings, "dhan_client_id", "")
            access_token = getattr(dhan_client, "access_token", None) or getattr(settings, "dhan_access_token", "")

            if not client_id or not access_token:
                log.warning("LiveFeedManager: missing client_id/access_token — feed disabled")
                return

            # Build pre-subscription instrument list (locked snapshot)
            with self._lock:
                pre_instruments = [
                    {"ExchangeSegment": "NSE_EQ", "SecurityId": sid}
                    for sid in self._subscriptions.values()
                ]

            self._feed = self._create_feed(broker, client_id, access_token, pre_instruments)
            if self._feed is None:
                return

            self._connected = True
            log.info("LiveFeedManager: WebSocket connected (LTP mode)")
            self._feed.run_forever()   # blocks until WebSocket closes

        except Exception as exc:
            log.warning("LiveFeedManager: feed thread error: %s", exc)
        finally:
            self._connected = False

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
        """Dispatch incoming tick to asyncio tick handler."""
        try:
            data = args[-1] if args else {}
            if not isinstance(data, dict):
                return
            security_id = str(
                data.get("security_id")
                or data.get("securityId")
                or data.get("SecurityId")
                or ""
            )
            ltp = float(
                data.get("LTP")
                or data.get("last_price")
                or data.get("lastPrice")
                or 0
            )
            if not security_id or ltp <= 0:
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
        log.warning("LiveFeedManager: WebSocket disconnected — monitor falls back to REST polling")

    def _on_error(self, error: Any) -> None:
        log.debug("LiveFeedManager: WebSocket error: %s", error)


# Module-level singleton
_feed_manager = LiveFeedManager()


def get_feed_manager() -> LiveFeedManager:
    return _feed_manager
