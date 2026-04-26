"""
yukti/agents/rag_schemas.py
Pydantic v2 schemas used by the RAG self-learning components.
"""
from __future__ import annotations

from typing import Optional, List
from datetime import datetime

from pydantic import BaseModel, Field


class JournalReflection(BaseModel):
    setup_summary: str
    outcome: str  # WIN | LOSS | BREAKEVEN
    reason: Optional[str] = None
    one_actionable_lesson: Optional[str] = None
    quality_score: int = Field(ge=0, le=10)
    market_regime: Optional[str] = None
    setup_type: Optional[str] = None
    created_at: Optional[datetime] = None


class RetrievedTradeContext(BaseModel):
    journal_id: Optional[int]
    trade_id: Optional[int]
    symbol: Optional[str]
    setup_type: Optional[str]
    direction: Optional[str]
    pnl_pct: Optional[float]
    similarity: Optional[float]
    quality_score: Optional[int]
    one_actionable_lesson: Optional[str]
    reason: Optional[str]
    created_at: Optional[datetime]
    retrieval_reason: Optional[str] = None


class RetrievalMetadata(BaseModel):
    retrieved_count: int
    avg_similarity: float
    avg_quality: float
    query_text: Optional[str] = None


class RagSettings(BaseModel):
    max_retrieved_items: int = 4
    recency_days: int = 90
    min_quality_score: int = 6
    outcome_weight_win: float = 1.2
    recency_half_life_days: int = 365
    max_fetch_candidates: int = 50
    diversity_lambda: float = 0.7
*** End Patch