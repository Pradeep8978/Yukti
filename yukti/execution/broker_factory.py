"""
yukti/execution/broker_factory.py
Broker dependency injection.

Routes orders to the correct broker based on MODE:
    live    → real DhanHQ (real money)
    paper   → PaperBroker simulating fills against live price feed
    shadow  → LIVE DhanHQ for market data, but all orders go to ShadowBroker
              which logs what WOULD have been done without placing real orders.
              Use shadow mode in parallel with paper mode for 1-2 weeks
              to verify decision quality without any risk.
    backtest → PaperBroker fed by historical candles (no live feed)

Usage:
    from yukti.execution.broker_factory import get_broker
    broker = get_broker()  # returns the right thing based on settings.mode
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from yukti.config import settings

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  ShadowBroker — logs what would have been done, never executes
# ═══════════════════════════════════════════════════════════════

class ShadowBroker:
    """
    Wraps the REAL DhanHQ client for read-only operations (market data,
    position fetching, candle data), but all WRITE operations (place_order,
    place_gtt, cancel, market_exit) are logged to a JSONL file and return
    a fake success response.

    Perfect for running a new strategy in production without risking capital:
    the agent sees real market conditions and real prices, but no real orders
    are placed. After 2 weeks, analyse shadow_orders.jsonl vs what paper
    trading would have done.
    """

    _ORDER_COUNTER = 0

    def __init__(self, real_dhan_client) -> None:
        self._real = real_dhan_client
        Path("logs").mkdir(exist_ok=True)
        self._log_path = "logs/shadow_orders.jsonl"
        log.info("ShadowBroker active — real market data, simulated orders")

    # ── PASS-THROUGH: market data reads ───────────────────────────────────────

    async def get_candles(self, *args, **kwargs):
        return await self._real.get_candles(*args, **kwargs)

    async def get_positions(self):
        """Return empty — shadow mode has no real positions."""
        return []

    async def get_order_list(self):
        return []

    # ── LOG-AND-FAKE: write operations ────────────────────────────────────────

    def _next_id(self, prefix: str = "SHADOW") -> str:
        ShadowBroker._ORDER_COUNTER += 1
        return f"{prefix}-{ShadowBroker._ORDER_COUNTER:06d}"

    def _log(self, operation: str, **kwargs) -> None:
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "operation": operation,
            **kwargs,
        }
        with open(self._log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    async def place_order(
        self,
        security_id:      str,
        transaction_type: str,
        quantity:         int,
        order_type:       str,
        product_type:     str,
        price:            float = 0.0,
        trigger_price:    float = 0.0,
        tag:              str = "",
    ) -> dict[str, Any]:
        order_id = self._next_id("SHADOW")
        self._log(
            "place_order",
            order_id=order_id, security_id=security_id,
            side=transaction_type, qty=quantity,
            order_type=order_type, product=product_type,
            price=price, tag=tag,
        )
        log.info("SHADOW place_order: %s %s qty=%d @ ₹%.2f",
                 transaction_type, security_id, quantity, price)
        # Fake a successful fill
        return {
            "orderId":      order_id,
            "orderStatus":  "TRADED",
            "filledQty":    quantity,
            "averagePrice": price,
        }

    async def place_gtt(
        self,
        security_id:      str,
        transaction_type: str,
        quantity:         int,
        trigger_price:    float,
        order_type:       str,
        product_type:     str,
        price:            float = 0.0,
    ) -> dict[str, Any]:
        gtt_id = self._next_id("SHADOW-GTT")
        self._log(
            "place_gtt",
            gtt_id=gtt_id, security_id=security_id,
            side=transaction_type, qty=quantity,
            trigger=trigger_price, order_type=order_type, product=product_type,
        )
        log.info("SHADOW place_gtt: %s trigger=%.2f qty=%d",
                 transaction_type, trigger_price, quantity)
        return {"gttOrderId": gtt_id, "status": "ACTIVE"}

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._log("cancel_order", order_id=order_id)
        return {"orderId": order_id, "orderStatus": "CANCELLED"}

    async def cancel_gtt(self, gtt_id: str) -> dict[str, Any]:
        self._log("cancel_gtt", gtt_id=gtt_id)
        return {"gttOrderId": gtt_id, "status": "CANCELLED"}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        # Shadow orders always report filled immediately
        return {"orderStatus": "TRADED", "filledQty": 100, "averagePrice": 0.0}

    async def market_exit(
        self,
        security_id: str,
        direction:   str,
        quantity:    int,
        product_type: str,
    ) -> dict[str, Any]:
        order_id = self._next_id("SHADOW-EXIT")
        self._log(
            "market_exit",
            order_id=order_id, security_id=security_id,
            direction=direction, quantity=quantity, product=product_type,
        )
        log.info("SHADOW market_exit: %s %s qty=%d", direction, security_id, quantity)
        return {"orderId": order_id, "orderStatus": "TRADED"}


# ═══════════════════════════════════════════════════════════════
#  PaperBrokerWrapper — real data reads, simulated order writes
# ═══════════════════════════════════════════════════════════════

class PaperBrokerWrapper:
    """
    Wraps real DhanClient for market data reads, PaperBroker for order writes.
    Perfect for paper trading: decisions based on real market conditions, but no real orders placed.
    """

    def __init__(self, real_dhan_client, paper_broker) -> None:
        self._real = real_dhan_client
        self._paper = paper_broker

    # ── PASS-THROUGH: market data reads from real DhanHQ ─────────────────────

    async def get_candles(self, *args, **kwargs):
        return await self._real.get_candles(*args, **kwargs)

    async def get_positions(self):
        return await self._real.get_positions()

    async def get_order_list(self):
        return await self._real.get_order_list()

    # ── SIMULATED: order writes via PaperBroker ──────────────────────────────

    async def place_order(self, *args, **kwargs):
        return await self._paper.place_order(*args, **kwargs)

    async def place_gtt(self, *args, **kwargs):
        return await self._paper.place_gtt(*args, **kwargs)

    async def cancel_order(self, *args, **kwargs):
        return await self._paper.cancel_order(*args, **kwargs)

    async def cancel_gtt(self, *args, **kwargs):
        return await self._paper.cancel_gtt(*args, **kwargs)

    async def get_order_status(self, *args, **kwargs):
        return await self._paper.get_order_status(*args, **kwargs)

    async def market_exit(self, *args, **kwargs):
        return await self._paper.market_exit(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════
#  Factory — returns the right broker for the current mode
# ═══════════════════════════════════════════════════════════════

_broker_instance = None


def get_broker():
    """
    Returns the broker instance matching the current MODE setting.
    Call once at startup; all subsequent calls return the same instance.
    """
    global _broker_instance
    if _broker_instance is not None:
        return _broker_instance

    mode = settings.mode.lower()

    if mode == "live":
        from yukti.execution.dhan_client import DhanClient
        _broker_instance = DhanClient()
        log.warning("⚠ LIVE MODE — real orders will be placed on DhanHQ")

    elif mode == "paper":
        from yukti.execution.dhan_client import DhanClient
        from yukti.backtest import PaperBroker
        real = DhanClient()
        paper = PaperBroker(settings.account_value)
        _broker_instance = PaperBrokerWrapper(real, paper)
        log.info("PAPER MODE — real market data, simulated fills, no real orders")

    elif mode == "shadow":
        from yukti.execution.dhan_client import DhanClient
        real   = DhanClient()
        _broker_instance = ShadowBroker(real)
        log.info("SHADOW MODE — real market data, decisions logged, no real orders")

    elif mode == "backtest":
        from yukti.backtest.paper_broker import PaperBroker
        _broker_instance = PaperBroker(settings.account_value)
        log.info("BACKTEST MODE — historical replay")

    else:
        raise ValueError(f"Unknown MODE: {mode}. Use live | paper | shadow | backtest")

    return _broker_instance


def reset_broker() -> None:
    """For tests only — clears the singleton so a fresh broker can be created."""
    global _broker_instance
    _broker_instance = None
