import pytest
from datetime import datetime, timedelta


def test_build_why_selected_same_symbol_win_recent():
    """_build_why_selected returns human-readable explanation."""
    from yukti.agents.memory import _build_why_selected

    reason = _build_why_selected(
        row_sym="RELIANCE",
        row_setup="orb_breakout",
        query_sym="RELIANCE",
        query_setup="orb_breakout",
        outcome="WIN",
        pnl_pct=2.5,
        age_days=10,
        qscore=9.0,
        base_sim=0.88,
        recency_days=90,
    )
    assert "same symbol" in reason
    assert "same setup" in reason
    assert "winning trade" in reason
    assert "recent" in reason
    assert "high quality" in reason
    assert "0.88" in reason


def test_build_why_selected_different_symbol_loss_old():
    from yukti.agents.memory import _build_why_selected

    reason = _build_why_selected(
        row_sym="INFY",
        row_setup="vwap_bounce",
        query_sym="TCS",
        query_setup="orb_breakout",
        outcome="LOSS",
        pnl_pct=-1.5,
        age_days=120,
        qscore=6.0,
        base_sim=0.75,
        recency_days=90,
    )
    assert "similar setup" in reason
    assert "loss trade" in reason
    # 120d is outside recency window, should not say 'recent'
    assert "recent" not in reason
    assert "120d ago" in reason
    # quality 6 is not "high quality"
    assert "high quality" not in reason


@pytest.mark.asyncio
async def test_retrieve_similar_formats_results(monkeypatch):
    """Mock embeddings and DB to validate formatted retrieval output."""
    from yukti.agents import memory

    async def fake_embed(texts, input_type="query"):
        return [[0.1, 0.2, 0.3]]

    class FakeRow:
        def __init__(self):
            self.entry_text = "sample journal text"
            self.pnl_pct = 1.5
            self.setup_type = "ORB"
            self.direction = "LONG"
            self.symbol = "ABC"
            self.similarity = 0.87

    def fake_get_db():
        class DBCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def execute(self, sql, params):
                class Res:
                    def fetchall(self):
                        return [FakeRow()]

                return Res()

        return DBCtx()

    monkeypatch.setattr(memory, "_embed", fake_embed)
    monkeypatch.setattr("yukti.data.database.get_db", fake_get_db)

    out = await memory.retrieve_similar("ABC", "ORB", "LONG", top_k=1)
    assert out
    assert "Past Similar Trades" in out
    assert "ABC" in out


@pytest.mark.asyncio
async def test_retrieve_similar_embed_failure_returns_empty(monkeypatch):
    from yukti.agents import memory

    async def fake_embed(texts, input_type="query"):
        raise RuntimeError("embed fail")

    monkeypatch.setattr(memory, "_embed", fake_embed)

    # Ensure DB fallback returns no rows so function yields empty string
    def fake_get_db_empty():
        class DBCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def execute(self, sql, params):
                class Res:
                    def fetchall(self):
                        return []

                return Res()

        return DBCtx()

    monkeypatch.setattr("yukti.data.database.get_db", fake_get_db_empty)

    out = await memory.retrieve_similar("ABC", "ORB", "LONG")
    assert out == ""


@pytest.mark.asyncio
async def test_retrieve_similar_hybrid_returns_structured_results(monkeypatch):
    """Test hybrid retrieval returns RetrievedJournal objects with metadata."""
    from yukti.agents import memory

    async def fake_embed(texts, input_type="query"):
        return [[0.1, 0.2, 0.3]]

    class FakeRow:
        def __init__(self):
            self.id = 1
            self.trade_id = 100
            self.symbol = "ABC"
            self.setup_type = "ORB"
            self.direction = "LONG"
            self.pnl_pct = 2.5
            self.entry_text = "Test journal entry"
            self.setup_summary = "Breakout setup"
            self.outcome = "WIN"
            self.reason = "Strong momentum"
            self.one_actionable_lesson = "Wait for volume confirmation"
            self.quality_score = 8.5
            self.market_regime = "BULLISH"
            self.is_high_conviction = True
            self.created_at = datetime.utcnow() - timedelta(days=10)
            self.base_similarity = 0.85
            self.weeks_old = 1.5

    def fake_get_db():
        class DBCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def execute(self, sql, params):
                class Res:
                    def fetchall(self):
                        return [FakeRow()]

                return Res()

        return DBCtx()

    monkeypatch.setattr(memory, "_embed", fake_embed)
    monkeypatch.setattr("yukti.data.database.get_db", fake_get_db)

    results = await memory.retrieve_similar_hybrid("ABC", "ORB", "LONG", market_regime="BULLISH")
    
    assert len(results) == 1
    assert results[0].symbol == "ABC"
    assert results[0].outcome == "WIN"
    assert results[0].quality_score == 8.5
    assert results[0].is_high_conviction is True
    assert results[0].similarity == 0.85


@pytest.mark.asyncio
async def test_retrieve_similar_hybrid_embed_failure_returns_empty(monkeypatch):
    """Test graceful fallback when embedding fails."""
    from yukti.agents import memory

    async def fake_embed(texts, input_type="query"):
        raise RuntimeError("embed fail")

    monkeypatch.setattr(memory, "_embed", fake_embed)

    results = await memory.retrieve_similar_hybrid("ABC", "ORB", "LONG")
    assert results == []


@pytest.mark.asyncio
async def test_format_retrieved_journals_for_context(monkeypatch):
    """Test context formatting includes all required fields."""
    from yukti.agents.memory import RetrievedJournal, format_retrieved_journals_for_context
    from datetime import datetime

    journals = [
        RetrievedJournal(
            trade_id=1,
            symbol="ABC",
            setup_type="ORB",
            direction="LONG",
            pnl_pct=2.5,
            entry_text="Full journal text here",
            setup_summary="Breakout above resistance",
            outcome="WIN",
            reason="Strong volume and momentum",
            one_actionable_lesson="Wait for retest confirmation",
            quality_score=8.5,
            market_regime="BULLISH",
            is_high_conviction=True,
            similarity=0.92,
            created_at=datetime.utcnow(),
            why_selected="similarity=0.92, winning trade",
        ),
        RetrievedJournal(
            trade_id=2,
            symbol="DEF",
            setup_type="VWAP",
            direction="SHORT",
            pnl_pct=-1.2,
            entry_text="Another journal",
            setup_summary="VWAP rejection",
            outcome="LOSS",
            reason="Failed to hold VWAP",
            one_actionable_lesson="Use tighter stop",
            quality_score=7.0,
            market_regime="BEARISH",
            is_high_conviction=False,
            similarity=0.78,
            created_at=datetime.utcnow(),
            why_selected="similarity=0.78",
        ),
    ]

    formatted = format_retrieved_journals_for_context(journals, include_meta_lessons=True)

    # Header
    assert "=== Past Similar Trades for Learning ===" in formatted
    # Entry 1: new header format is "1. SYMBOL DIR | setup | OUTCOME pnl%"
    assert "ABC" in formatted
    assert "ORB" in formatted
    assert "WIN" in formatted
    # Lesson line uses new label
    assert "Lesson  :" in formatted
    assert "Wait for retest confirmation" in formatted
    # Sim line
    assert "Sim=0.92" in formatted
    # Entry 2
    assert "DEF" in formatted
    assert "LOSS" in formatted
    assert "Use tighter stop" in formatted
    # meta_lessons.json does not exist in test env → section silently omitted (no assertion needed)


@pytest.mark.asyncio
async def test_retrieve_similar_hybrid_diversity_filtering(monkeypatch):
    """Test that diversity filtering limits same-outcome results."""
    from yukti.agents import memory

    async def fake_embed(texts, input_type="query"):
        return [[0.1, 0.2, 0.3]]

    # Create 5 rows: 3 WIN, 2 LOSS
    class FakeRow:
        def __init__(self, idx, outcome, quality=7.0, high_conv=False):
            self.id = idx
            self.trade_id = 100 + idx
            self.symbol = f"SYM{idx}"
            self.setup_type = "ORB"
            self.direction = "LONG"
            self.pnl_pct = 2.5 if outcome == "WIN" else -1.5
            self.entry_text = f"Journal {idx}"
            self.setup_summary = "Test"
            self.outcome = outcome
            self.reason = "Test reason"
            self.one_actionable_lesson = "Test lesson"
            self.quality_score = quality
            self.market_regime = "BULLISH"
            self.is_high_conviction = high_conv
            self.created_at = datetime.utcnow() - timedelta(days=10)
            self.base_similarity = 0.9 - (idx * 0.05)
            self.weeks_old = 1.5

    def fake_get_db():
        class DBCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def execute(self, sql, params):
                class Res:
                    def fetchall(self):
                        # 3 WIN, 2 LOSS
                        return [
                            FakeRow(1, "WIN"),
                            FakeRow(2, "WIN"),
                            FakeRow(3, "WIN"),
                            FakeRow(4, "LOSS"),
                            FakeRow(5, "LOSS"),
                        ]

                return Res()

        return DBCtx()

    monkeypatch.setattr(memory, "_embed", fake_embed)
    monkeypatch.setattr("yukti.data.database.get_db", fake_get_db)

    # Request 4 results - should get max 2 from same outcome
    results = await memory.retrieve_similar_hybrid("ABC", "ORB", "LONG", top_k=4)
    
    # Should have at most 4 results
    assert len(results) <= 4
    
    # Count outcomes
    outcomes = [r.outcome for r in results]
    win_count = outcomes.count("WIN")
    loss_count = outcomes.count("LOSS")
    
    # Should not have more than 2 of each (diversity limit)
    assert win_count <= 2
    assert loss_count <= 2
