"""tests/unit/test_universe_scanner.py — tests for scanner scoring and selection logic."""
from __future__ import annotations

import pytest

from yukti.services.universe_scanner_service import (
    _score_candidate,
    _deduplicate_candidates,
    _select_universe,
)


def test_score_candidate_caps_at_100():
    c = {
        "vol_ratio": 5, "change_pct": 4,
        "has_catalyst": True, "sector_in_play": True,
        "avg_turnover_cr": 100,
        "gap_pct": 5.0, "atr_pct_rank": 1.0, "score_boost": 10,
    }
    score = _score_candidate(c)
    assert score == 100.0


def test_deduplicate_candidates_keeps_highest():
    a = {"symbol": "ABC", "vol_ratio": 1}
    b = {"symbol": "ABC", "vol_ratio": 5}
    res = _deduplicate_candidates([a, b])
    assert len(res) == 1
    assert res[0]["symbol"] == "ABC"


def test_select_universe_includes_existing_positions():
    candidates = [
        {"symbol": "A", "avg_turnover_cr": 20, "vol_ratio": 2, "change_pct": 1, "security_id": "1"},
        {"symbol": "B", "avg_turnover_cr": 20, "vol_ratio": 1, "change_pct": 2, "security_id": "2"},
    ]
    selected = _select_universe(candidates, pick_count=1, min_turnover_cr=10, existing_positions=["B"])
    assert any(c["symbol"] == "B" for c in selected)


class TestScoring:
    def test_score_volume_surge(self):
        from yukti.services.universe_scanner_service import _score_candidate
        candidate = {
            "symbol": "RELIANCE", "security_id": "1333",
            "vol_ratio": 3.0, "change_pct": 1.0,
            "has_catalyst": False, "sector_in_play": False,
            "avg_turnover_cr": 100,
        }
        score = _score_candidate(candidate)
        assert 0 <= score <= 100
        # vol_ratio=3 → min(3/5,1)*25 = 15.0
        assert score >= 15

    def test_score_caps_at_100(self):
        from yukti.services.universe_scanner_service import _score_candidate
        candidate = {
            "symbol": "RELIANCE", "security_id": "1333",
            "vol_ratio": 10.0, "change_pct": 6.0,
            "has_catalyst": True, "sector_in_play": True,
            "avg_turnover_cr": 200,
            "gap_pct": 4.0, "atr_pct_rank": 1.0, "score_boost": 10,
        }
        score = _score_candidate(candidate)
        assert score == 100

    def test_score_with_catalyst(self):
        from yukti.services.universe_scanner_service import _score_candidate
        candidate = {
            "symbol": "TCS", "security_id": "11536",
            "vol_ratio": 1.0, "change_pct": 0.5,
            "has_catalyst": True, "sector_in_play": False,
            "avg_turnover_cr": 50,
        }
        score = _score_candidate(candidate)
        assert score >= 20  # catalyst alone is 20

    def test_liquidity_floor_rejects(self):
        from yukti.services.universe_scanner_service import _select_universe
        candidates = [
            {
                "symbol": "PENNY", "security_id": "9999",
                "vol_ratio": 5.0, "change_pct": 3.0,
                "has_catalyst": True, "sector_in_play": True,
                "avg_turnover_cr": 5,  # below 10 Cr threshold
            },
        ]
        selected = _select_universe(candidates, pick_count=15, min_turnover_cr=10)
        assert len(selected) == 0

    def test_selection_respects_pick_count(self):
        from yukti.services.universe_scanner_service import _select_universe
        candidates = [
            {
                "symbol": f"STOCK{i}", "security_id": str(i),
                "vol_ratio": 3.0, "change_pct": 2.0,
                "has_catalyst": False, "sector_in_play": False,
                "avg_turnover_cr": 50,
            }
            for i in range(30)
        ]
        selected = _select_universe(candidates, pick_count=10, min_turnover_cr=10)
        assert len(selected) == 10

    def test_selection_sorted_by_score_desc(self):
        from yukti.services.universe_scanner_service import _select_universe
        candidates = [
            {
                "symbol": "LOW", "security_id": "1",
                "vol_ratio": 1.0, "change_pct": 0.5,
                "has_catalyst": False, "sector_in_play": False,
                "avg_turnover_cr": 50,
            },
            {
                "symbol": "HIGH", "security_id": "2",
                "vol_ratio": 5.0, "change_pct": 4.0,
                "has_catalyst": True, "sector_in_play": True,
                "avg_turnover_cr": 200,
            },
        ]
        selected = _select_universe(candidates, pick_count=15, min_turnover_cr=10)
        assert selected[0]["symbol"] == "HIGH"

    def test_no_duplicate_inflation(self):
        from yukti.services.universe_scanner_service import _deduplicate_candidates
        candidates = [
            {"symbol": "RELIANCE", "security_id": "1333", "vol_ratio": 3.0,
             "change_pct": 1.0, "has_catalyst": True, "sector_in_play": False,
             "avg_turnover_cr": 100},
            {"symbol": "RELIANCE", "security_id": "1333", "vol_ratio": 2.0,
             "change_pct": 2.0, "has_catalyst": False, "sector_in_play": True,
             "avg_turnover_cr": 100},
        ]
        deduped = _deduplicate_candidates(candidates)
        assert len(deduped) == 1
        assert deduped[0]["symbol"] == "RELIANCE"


class TestThresholdFloors:
    def test_volume_below_floor_scores_zero_for_volume(self):
        from yukti.services.universe_scanner_service import _score_candidate
        # vol_ratio just under default 2.0 floor → volume component must be 0
        c = {"vol_ratio": 1.5, "change_pct": 0, "has_catalyst": False,
             "sector_in_play": False, "avg_turnover_cr": 0}
        assert _score_candidate(c) == 0

    def test_price_below_floor_scores_zero_for_price(self):
        from yukti.services.universe_scanner_service import _score_candidate
        # change_pct just under 1.5 floor → price component is 0; volume kicks in.
        c = {"vol_ratio": 5.0, "change_pct": 1.0, "has_catalyst": False,
             "sector_in_play": False, "avg_turnover_cr": 50}
        # New weights: volume 20, liquidity 10, price 0 → 30 total
        assert _score_candidate(c) == 30.0


class TestPinnedWatchlist:
    def test_pinned_bypasses_liquidity_floor(self):
        from yukti.services.universe_scanner_service import _select_universe
        candidates = [
            {"symbol": "PINNED", "security_id": "1", "pinned": True,
             "vol_ratio": 0, "change_pct": 0,
             "has_catalyst": False, "sector_in_play": False,
             "avg_turnover_cr": 1},  # well below default 10 Cr floor
        ]
        selected = _select_universe(candidates, pick_count=15, min_turnover_cr=10)
        assert any(c["symbol"] == "PINNED" for c in selected)


class TestNewScoringFeatures:
    def test_gap_score_saturates_at_three_pct(self):
        from yukti.services.universe_scanner_service import _score_candidate
        # Pure gap signal, nothing else.
        c = {"vol_ratio": 0, "change_pct": 0, "gap_pct": 5.0,
             "has_catalyst": False, "sector_in_play": False, "avg_turnover_cr": 0}
        assert _score_candidate(c) == 10  # saturates at 3% → 10 pts

    def test_atr_rank_contributes_up_to_ten(self):
        from yukti.services.universe_scanner_service import _score_candidate
        c = {"vol_ratio": 0, "change_pct": 0, "atr_pct_rank": 1.0,
             "has_catalyst": False, "sector_in_play": False, "avg_turnover_cr": 0}
        assert _score_candidate(c) == 10

    def test_score_boost_caps_at_ten(self):
        from yukti.services.universe_scanner_service import _score_candidate
        c = {"vol_ratio": 0, "change_pct": 0, "score_boost": 50,
             "has_catalyst": False, "sector_in_play": False, "avg_turnover_cr": 0}
        assert _score_candidate(c) == 10

    def test_atr_percentile_annotates_in_place(self):
        from yukti.services.universe_scanner_service import _annotate_atr_percentile
        rows = [
            {"symbol": "LO", "atr_pct_pool": 1.6},
            {"symbol": "MID", "atr_pct_pool": 3.0},
            {"symbol": "HI", "atr_pct_pool": 5.5},
        ]
        _annotate_atr_percentile(rows)
        assert rows[0]["atr_pct_rank"] == 0.0
        assert rows[2]["atr_pct_rank"] == 1.0
        assert 0.0 < rows[1]["atr_pct_rank"] < 1.0


class TestSectorCapGate:
    @pytest.mark.asyncio
    async def test_sector_cap_blocks_when_projected_exceeds(self):
        from unittest.mock import patch, AsyncMock
        from yukti.risk import Portfolio, run_gates
        from yukti.agents.arjun import TradeDecision

        td = TradeDecision(
            symbol="HDFCBANK", action="TRADE", direction="LONG",
            reasoning="x", conviction=8,
            entry_price=1500.0, stop_loss=1490.0,
            target_1=1530.0, target_2=1560.0, risk_reward=2.0,
        )
        # Banking already at 38%; the cap is 40% → adding any meaningful slice
        # of capital tips it over.
        portfolio = Portfolio(
            account_value=500_000, open_positions=1, daily_pnl_pct=0.0,
            total_exposure_pct=38.0,
            sector_exposure_pct={"BANK": 38.0},
            trade_sector="BANK",
        )
        with (
            patch("yukti.risk.is_on_cooldown", new=AsyncMock(return_value=False)),
            patch("yukti.risk.is_market_halted", new=AsyncMock(return_value=False)),
        ):
            result = await run_gates(td, portfolio)
        assert result.passed is False
        # Either the single-stock cap or the sector cap should reject — both
        # are valid, deterministic outcomes for this scenario.
        assert "cap" in (result.reason or "")

    @pytest.mark.asyncio
    async def test_sector_cap_passes_when_no_sector_info(self):
        from unittest.mock import patch, AsyncMock
        from yukti.risk import Portfolio, run_gates
        from yukti.agents.arjun import TradeDecision

        # 5% stop keeps position size well within the 25% single-stock cap.
        td = TradeDecision(
            symbol="HDFCBANK", action="TRADE", direction="LONG",
            reasoning="x", conviction=7,
            entry_price=100.0, stop_loss=95.0,
            target_1=110.0, target_2=120.0, risk_reward=2.0,
        )
        # No sector info → sector cap gate is skipped (backward compatible).
        portfolio = Portfolio(
            account_value=500_000, open_positions=1, daily_pnl_pct=0.0,
            total_exposure_pct=10.0,
        )
        with (
            patch("yukti.risk.is_on_cooldown", new=AsyncMock(return_value=False)),
            patch("yukti.risk.is_market_halted", new=AsyncMock(return_value=False)),
        ):
            result = await run_gates(td, portfolio)
        assert result.passed is True
