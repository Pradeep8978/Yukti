"""
yukti/execution/order_intent.py
Pre-commits a trade's intended structure to PostgreSQL BEFORE placing any order.

This closes the critical race condition:
  1. Place entry order
  2. Wait for fill (5-120 seconds)                  ← CRASH HERE = naked position
  3. Register SL GTT                                ← OR CRASH HERE
  4. Register target GTT

Without persistence, a crash between steps 2 and 3 leaves an open position with no stop.
With order_intent, the morning reconcile job detects it and arms missing GTTs.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from yukti.data.database import Base, get_db

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  OrderIntent — a persisted "contract" for a trade
# ═══════════════════════════════════════════════════════════════

class OrderIntent(Base):
    """
    Persisted record of what we INTEND a trade to look like.
    Written BEFORE any DhanHQ call so we can recover from crashes.

    State progression:
        PLANNED     — intent saved, entry not yet placed
        PLACED      — entry order placed, awaiting fill
        FILLED      — entry filled, GTTs not yet armed
        ARMED       — both SL + target GTTs registered, fully protected
        CLOSED      — position closed normally
        ABANDONED   — intent cancelled before placement
        UNSAFE      — FILLED but GTT registration failed, needs manual review
    """
    __tablename__ = "order_intents"

    id:              Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol:          Mapped[str] = mapped_column(String(20), index=True)
    security_id:     Mapped[str] = mapped_column(String(20))
    direction:       Mapped[str] = mapped_column(String(5))
    holding_period:  Mapped[str] = mapped_column(String(10))

    quantity:        Mapped[int] = mapped_column(Integer)
    entry_price:     Mapped[float] = mapped_column(Float)
    stop_loss:       Mapped[float] = mapped_column(Float)
    target_1:        Mapped[float] = mapped_column(Float)
    target_2:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    conviction:      Mapped[int] = mapped_column(Integer)
    setup_type:      Mapped[str] = mapped_column(String(30))
    reasoning:       Mapped[str] = mapped_column(Text)

    # State machine
    state:           Mapped[str] = mapped_column(String(15), default="PLANNED", index=True)

    # IDs filled in as we progress through state
    entry_order_id:  Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    sl_gtt_id:       Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    target_gtt_id:   Mapped[Optional[str]] = mapped_column(String(60), nullable=True)

    # Fill info
    fill_price:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    filled_qty:      Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)

    # Timestamps
    created_at:      Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    placed_at:       Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    filled_at:       Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    armed_at:        Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at:       Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Audit — last error if transition failed
    last_error:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ═══════════════════════════════════════════════════════════════
#  Persistence helpers
# ═══════════════════════════════════════════════════════════════

async def save_intent(
    symbol:         str,
    security_id:    str,
    direction:      str,
    holding_period: str,
    quantity:       int,
    entry_price:    float,
    stop_loss:      float,
    target_1:       float,
    target_2:       Optional[float],
    conviction:     int,
    setup_type:     str,
    reasoning:      str,
) -> int:
    """Create the intent record. Returns the primary key id."""
    async with get_db() as db:
        intent = OrderIntent(
            symbol          = symbol,
            security_id     = security_id,
            direction       = direction,
            holding_period  = holding_period,
            quantity        = quantity,
            entry_price     = entry_price,
            stop_loss       = stop_loss,
            target_1        = target_1,
            target_2        = target_2,
            conviction      = conviction,
            setup_type      = setup_type,
            reasoning       = reasoning,
            state           = "PLANNED",
        )
        db.add(intent)
        await db.flush()
        intent_id = intent.id
        await db.commit()

    log.info("Intent #%d saved — PLANNED %s %s qty=%d @ ₹%.2f",
             intent_id, direction, symbol, quantity, entry_price)
    return intent_id


async def mark_placed(intent_id: int, entry_order_id: str) -> None:
    async with get_db() as db:
        intent = await db.get(OrderIntent, intent_id)
        if not intent:
            return
        intent.entry_order_id = entry_order_id
        intent.state          = "PLACED"
        intent.placed_at      = datetime.utcnow()
        await db.commit()


async def mark_filled(intent_id: int, fill_price: float, filled_qty: int) -> None:
    async with get_db() as db:
        intent = await db.get(OrderIntent, intent_id)
        if not intent:
            return
        intent.fill_price = fill_price
        intent.filled_qty = filled_qty
        intent.state      = "FILLED"
        intent.filled_at  = datetime.utcnow()
        await db.commit()


async def mark_armed(
    intent_id:      int,
    sl_gtt_id:      str,
    target_gtt_id:  Optional[str],
) -> None:
    async with get_db() as db:
        intent = await db.get(OrderIntent, intent_id)
        if not intent:
            return
        intent.sl_gtt_id     = sl_gtt_id
        intent.target_gtt_id = target_gtt_id
        intent.state         = "ARMED"
        intent.armed_at      = datetime.utcnow()
        await db.commit()


async def mark_closed(intent_id: int) -> None:
    async with get_db() as db:
        intent = await db.get(OrderIntent, intent_id)
        if not intent:
            return
        intent.state     = "CLOSED"
        intent.closed_at = datetime.utcnow()
        await db.commit()


async def mark_unsafe(intent_id: int, error: str) -> None:
    """Called when GTT registration fails after fill — needs manual review."""
    async with get_db() as db:
        intent = await db.get(OrderIntent, intent_id)
        if not intent:
            return
        intent.state      = "UNSAFE"
        intent.last_error = error
        await db.commit()
    log.critical("Intent #%d marked UNSAFE: %s", intent_id, error)


async def mark_abandoned(intent_id: int, reason: str) -> None:
    """Intent was created but entry placement failed or was cancelled."""
    async with get_db() as db:
        intent = await db.get(OrderIntent, intent_id)
        if not intent:
            return
        intent.state      = "ABANDONED"
        intent.last_error = reason
        await db.commit()


# ═══════════════════════════════════════════════════════════════
#  Recovery — called on startup by reconciliation
# ═══════════════════════════════════════════════════════════════

async def find_unsafe_intents() -> list[OrderIntent]:
    """
    Find intents in a dangerous state — FILLED but never ARMED.
    Called on startup by the reconciliation job.

    Recovery actions:
    - If broker actually has the position → re-arm GTTs
    - If broker doesn't have it → mark ABANDONED
    """
    async with get_db() as db:
        result = await db.execute(
            select(OrderIntent)
            .where(OrderIntent.state.in_(["PLACED", "FILLED"]))
        )
        return list(result.scalars().all())


async def find_stale_intents(older_than_minutes: int = 10) -> list[OrderIntent]:
    """
    Find intents stuck in PLACED state for too long.
    The entry order probably timed out without filling.
    """
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=older_than_minutes)
    async with get_db() as db:
        result = await db.execute(
            select(OrderIntent)
            .where(
                OrderIntent.state == "PLACED",
                OrderIntent.placed_at < cutoff,
            )
        )
        return list(result.scalars().all())
