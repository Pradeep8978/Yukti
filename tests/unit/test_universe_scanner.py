"""tests/unit/test_universe_scanner.py — tests for scanner scoring and selection logic."""
from __future__ import annotations

import pytest


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
