"""
yukti/data/models.py
All SQLAlchemy ORM models for Yukti.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, func, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from yukti.data.database import Base


# ─────────────────────────────────────────────────────────────
# Trade
# ─────────────────────────────────────────────────────────────

class Trade(Base):
    """One full trade lifecycle: entry → armed → closed."""

    __tablename__ = "trades"

    id:              Mapped[int]            = mapped_column(primary_key=True, autoincrement=True)
    symbol:          Mapped[str]            = mapped_column(String(20), index=True)
    security_id:     Mapped[str]            = mapped_column(String(20))
    exchange:        Mapped[str]            = mapped_column(String(10), default="NSE_EQ")

    direction:       Mapped[str]            = mapped_column(String(5))   # LONG | SHORT
    setup_type:      Mapped[str]            = mapped_column(String(30))
    holding_period:  Mapped[str]            = mapped_column(String(10))  # intraday | swing
    market_bias:     Mapped[str]            = mapped_column(String(10))  # BULLISH | BEARISH | NEUTRAL

    entry_price:     Mapped[float]          = mapped_column(Float)
    stop_loss:       Mapped[float]          = mapped_column(Float)
    target_1:        Mapped[float]          = mapped_column(Float)
    target_2:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quantity:        Mapped[int]            = mapped_column(Integer)
    conviction:      Mapped[int]            = mapped_column(Integer)
    risk_reward:     Mapped[float]          = mapped_column(Float)
    max_loss:        Mapped[float]          = mapped_column(Float)

    # DhanHQ order IDs
    entry_order_id:  Mapped[Optional[str]]  = mapped_column(String(60), nullable=True)
    sl_gtt_id:       Mapped[Optional[str]]  = mapped_column(String(60), nullable=True)
    target_gtt_id:   Mapped[Optional[str]]  = mapped_column(String(60), nullable=True)

    # State machine
    status: Mapped[str] = mapped_column(String(15), default="PENDING", index=True)
    # PENDING → FILLED → ARMED → CLOSED | SQUAREDOFF | CANCELLED

    # Outcome
    exit_price:      Mapped[Optional[float]] = mapped_column(Float,    nullable=True)
    exit_reason:     Mapped[Optional[str]]   = mapped_column(String(30), nullable=True)
    pnl:             Mapped[Optional[float]] = mapped_column(Float,    nullable=True)
    pnl_pct:         Mapped[Optional[float]] = mapped_column(Float,    nullable=True)

    # Reasoning
    reasoning:       Mapped[str]  = mapped_column(Text)

    # Timestamps
    opened_at:  Mapped[datetime]          = mapped_column(DateTime, server_default=func.now())
    filled_at:  Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at:  Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationship
    journal_entries: Mapped[list["JournalEntry"]] = relationship(back_populates="trade")


# ─────────────────────────────────────────────────────────────
# JournalEntry
# ─────────────────────────────────────────────────────────────

class JournalEntry(Base):
    """Post-trade reflection written by Claude. Stored with vector embedding for memory retrieval.
    
    Enhanced schema includes structured fields for hybrid retrieval and quality scoring.
    """

    __tablename__ = "journal_entries"

    id:         Mapped[int]  = mapped_column(primary_key=True, autoincrement=True)
    trade_id:   Mapped[int]  = mapped_column(ForeignKey("trades.id"), index=True)
    symbol:     Mapped[str]  = mapped_column(String(20), index=True)
    setup_type: Mapped[str]  = mapped_column(String(30))
    direction:  Mapped[str]  = mapped_column(String(5))
    pnl_pct:    Mapped[float] = mapped_column(Float)
    entry_text: Mapped[str]  = mapped_column(Text)

    # Enhanced structured fields for hybrid retrieval
    setup_summary:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome:            Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # WIN | LOSS | BREAKEVEN
    reason:             Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quality_score:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0-10 self-score
    market_regime:      Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # BULLISH | BEARISH | NEUTRAL | VOLATILE
    is_high_conviction: Mapped[bool] = mapped_column(default=False)  # conviction >= 8
    
    # Legacy/Refinement fields
    key_lesson:         Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    outcome_reason:     Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    one_actionable_lesson: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discarded:          Mapped[bool] = mapped_column(Boolean, default=False)

    # 1024-dim Voyage AI embedding
    embedding:  Mapped[Optional[list[float]]] = mapped_column(Vector(1024), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    trade: Mapped["Trade"] = relationship(back_populates="journal_entries")


# ─────────────────────────────────────────────────────────────
# DecisionLog
# ─────────────────────────────────────────────────────────────

class DecisionLog(Base):
    """Immutable log of every Claude decision — trade or skip. Used for audit + debugging."""

    __tablename__ = "decision_log"

    id:          Mapped[int]            = mapped_column(primary_key=True, autoincrement=True)
    symbol:      Mapped[str]            = mapped_column(String(20), index=True)
    action:      Mapped[str]            = mapped_column(String(5))   # TRADE | SKIP
    direction:   Mapped[Optional[str]]  = mapped_column(String(5), nullable=True)
    market_bias: Mapped[Optional[str]]  = mapped_column(String(10), nullable=True)
    conviction:  Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    reasoning:   Mapped[str]            = mapped_column(Text)
    skip_reason: Mapped[Optional[str]]  = mapped_column(String(60), nullable=True)
    full_json:   Mapped[dict]           = mapped_column(JSONB)        # raw Claude output
    decided_at:  Mapped[datetime]       = mapped_column(DateTime, server_default=func.now(), index=True)


# ─────────────────────────────────────────────────────────────
# DailyPerformance
# ─────────────────────────────────────────────────────────────

class DailyPerformance(Base):
    """Aggregated daily stats. Written at EOD by the scheduler."""

    __tablename__ = "daily_performance"

    date:            Mapped[date]  = mapped_column(Date, primary_key=True)
    trades_taken:    Mapped[int]   = mapped_column(Integer, default=0)
    trades_won:      Mapped[int]   = mapped_column(Integer, default=0)
    trades_lost:     Mapped[int]   = mapped_column(Integer, default=0)
    gross_pnl:       Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate:        Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor:   Mapped[float] = mapped_column(Float, default=0.0)


# ─────────────────────────────────────────────────────────────
# OHLCV candle cache (TimescaleDB hypertable)
# ─────────────────────────────────────────────────────────────

class Candle(Base):
    """Historical OHLCV candles. Created as a TimescaleDB hypertable on time column."""

    __tablename__ = "candles"

    id:       Mapped[int]   = mapped_column(primary_key=True, autoincrement=True)
    symbol:   Mapped[str]   = mapped_column(String(20), index=True)
    interval: Mapped[str]   = mapped_column(String(5))   # "1", "5", "15", "D"
    time:     Mapped[datetime] = mapped_column(DateTime, index=True)
    open:     Mapped[float] = mapped_column(Float)
    high:     Mapped[float] = mapped_column(Float)
    low:      Mapped[float] = mapped_column(Float)
    close:    Mapped[float] = mapped_column(Float)
    volume:   Mapped[float] = mapped_column(Float)


# ─────────────────────────────────────────────────────────────
# Position
# ─────────────────────────────────────────────────────────────

class Position(Base):
    """Authoritative in-memory position record for open positions.

    Stored in Postgres and cached in Redis by `yukti.data.state`.
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    security_id: Mapped[str] = mapped_column(String(20))
    intent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_intents.id"), nullable=True)

    direction: Mapped[str] = mapped_column(String(5))
    setup_type: Mapped[str] = mapped_column(String(30))
    holding_period: Mapped[str] = mapped_column(String(10))

    entry_price: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float)
    target_1: Mapped[float] = mapped_column(Float)
    target_2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    quantity: Mapped[int] = mapped_column(Integer)
    conviction: Mapped[int] = mapped_column(Integer)
    risk_reward: Mapped[float] = mapped_column(Float)

    entry_order_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    sl_gtt_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    target_gtt_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)

    status: Mapped[str] = mapped_column(String(15), default="OPEN", index=True)
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

# Register OrderIntent model with Base.metadata
from yukti.execution.order_intent import OrderIntent  # noqa: F401

