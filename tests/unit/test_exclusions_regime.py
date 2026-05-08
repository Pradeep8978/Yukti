"""tests/unit/test_exclusions_regime.py — slice 4 hygiene services."""
from __future__ import annotations

import pytest

from yukti.services.exclusions_service import Exclusions, filter_candidates
from yukti.services.regime_service import classify_vix, Regime


class TestExclusions:
    def test_reasons_for_collects_all_buckets(self):
        e = Exclusions(fno_ban={"X"}, asm={"X"}, gsm={"Y"}, at_circuit={"Z"})
        assert set(e.reasons_for("X")) == {"fno_ban", "asm"}
        assert e.reasons_for("Y") == ["gsm"]
        assert e.reasons_for("Z") == ["at_circuit"]
        assert e.reasons_for("ABSENT") == []

    def test_filter_drops_excluded_and_counts_reasons(self):
        e = Exclusions(fno_ban={"BAN"}, gsm={"GSM"}, at_circuit={"CIRC"})
        candidates = [
            {"symbol": "BAN"},
            {"symbol": "GSM"},
            {"symbol": "CIRC"},
            {"symbol": "OK"},
        ]
        kept, drops = filter_candidates(candidates, e)
        assert [c["symbol"] for c in kept] == ["OK"]
        # Each got dropped for its reason
        assert drops.get("fno_ban", 0) == 1
        assert drops.get("gsm", 0) == 1
        assert drops.get("at_circuit", 0) == 1


class TestRegime:
    def test_classify_vix_buckets(self):
        bucket, depth, bump = classify_vix(10.0)
        assert bucket == "calm"
        assert depth == 1.0
        assert bump == 0

        bucket, depth, bump = classify_vix(15.0)
        assert bucket == "normal"
        assert bump == 0

        bucket, depth, bump = classify_vix(20.0)
        assert bucket == "elevated"
        assert depth == 0.6
        assert bump == 1

        bucket, depth, bump = classify_vix(30.0)
        assert bucket == "stressed"
        assert depth == 0.3
        assert bump == 2

    def test_classify_vix_none_defaults_normal(self):
        bucket, depth, bump = classify_vix(None)
        assert bucket == "normal"
        assert depth == 1.0
        assert bump == 0

    def test_classify_vix_custom_buckets(self):
        # Tighter regime thresholds.
        bucket, _, _ = classify_vix(13.0, buckets=[10, 14, 20])
        assert bucket == "normal"
        bucket, _, _ = classify_vix(15.0, buckets=[10, 14, 20])
        assert bucket == "elevated"
