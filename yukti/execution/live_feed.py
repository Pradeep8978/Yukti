"""
yukti/execution/live_feed.py
WebSocket live feed manager for real-time LTP ticks from DhanHQ.

Runs DhanFeed in a background daemon thread; dispatches incoming ticks
back to the asyncio event loop via asyncio.run_coroutine_threadsafe().

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
        self._loop = asyncio.get_event_loop()

        self._thread = threading.Thread(
            target=self._run_feed, daemon=True, name="dhan-live-feed"
        )
        self._thread.start()
        log.info("LiveFeedManager: WebSocket thread started")

    async def subscribe(self, symbol: str, security_id: str) -> None:
        """Register a symbol for LTP ticks."""
        if security_id in self._security_to_symbol:
            return
        self._subscriptions[symbol] = security_id
        self._security_to_symbol[security_id] = symbol
        log.debug("LiveFeedManager: subscribed %s (id=%s)", symbol, security_id)

        if self._connected and self._feed is not None:
            try:
                self._feed.subscribe_symbols(
                    [{"ExchangeSegment": "NSE_EQ", "SecurityId": security_id}]
                )
            except Exception as exc:
                log.debug("LiveFeedManager: subscribe_symbols failed for %s: %s", symbol, exc)

    async def unsubscribe(self, symbol: str) -> None:
        """Remove a symbol from LTP ticks."""
        security_id = self._subscriptions.pop(symbol, None)
        if not security_id:
            return
        self._security_to_symbol.pop(security_id, None)
        log.debug("LiveFeedManager: unsubscribed %s", symbol)

        if self._connected and self._feed is not None:
            try:
                self._feed.unsubscribe_symbols(
                    [{"ExchangeSegment": "NSE_EQ", "SecurityId": security_id}]
                )
            except Exception as exc:
                log.debug("LiveFeedManager: unsubscribe_symbols failed for %s: %s", symbol, exc)

    def is_connected(self) -> bool:
        return self._connected

    # ── Internal ─────────────────────────────────────────────────

    def _run_feed(self) -> None:
        """Blocking run in daemon thread — creates DhanFeed and calls run_forever()."""
        try:
            from dhanhq import DhanFeed
            from yukti.config import settings
            from yukti.execution.broker_factory import get_broker

            broker = get_broker()
            dhan_client = getattr(broker, "_dhan", None)

            client_id    = getattr(dhan_client, "client_id",    None) or getattr(settings, "dhan_client_id", "")
            access_token = getattr(dhan_client, "access_token", None) or getattr(settings, "dhan_access_token", "")

            if not client_id or not access_token:
                log.warning("LiveFeedManager: missing client_id/access_token — feed disabled")
                return

            self._feed = DhanFeed(
                client_id         = client_id,
                access_token      = access_token,
                subscription_code = DhanFeed.LTP,
                on_message        = self._on_message,
                on_close          = self._on_close,
                on_error          = self._on_error,
            )
            self._connected = True
            log.info("LiveFeedManager: WebSocket connected (LTP mode)")

            # Subscribe any symbols registered before connection
            if self._subscriptions:
                try:
                    instruments = [
                        {"ExchangeSegment": "NSE_EQ", "SecurityId": sid}
                        for sid in self._subscriptions.values()
                    ]
                    self._feed.subscribe_symbols(instruments)
                    log.info("LiveFeedManager: pre-subscribed %d symbols", len(instruments))
                except Exception as exc:
                    log.debug("LiveFeedManager: pre-connect subscription failed: %s", exc)

            self._feed.run_forever()   # blocks until WebSocket closes

        except ImportError:
            log.warning("LiveFeedManager: dhanhq package missing — WebSocket feed disabled")
        except Exception as exc:
            log.warning("LiveFeedManager: feed thread error: %s", exc)
        finally:
            self._connected = False

    def _on_message(self, data: dict) -> None:
        """Dispatch incoming tick to asyncio tick handler."""
        try:
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
