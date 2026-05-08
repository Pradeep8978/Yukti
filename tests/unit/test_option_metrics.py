"""tests/unit/test_option_metrics.py — unit tests for option chain metric functions."""
from __future__ import annotations

import pytest

from yukti.services.option_metrics_service import (
    compute_pcr,
    compute_max_pain,
    compute_atm_iv,
    _pcr_to_sentiment,
    _nearest_expiry,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strike(k: float, call_oi: int = 0, put_oi: int = 0, call_iv: float = 0.0) -> dict:
    """Build a minimal option chain row in DhanHQ call_options/put_options shape."""
    return {
        "strike_price": k,
        "call_options": {"open_interest": call_oi, "implied_volatility": call_iv},
        "put_options":  {"open_interest": put_oi},
    }


def _strike_ce_pe(k: float, call_oi: int = 0, put_oi: int = 0, call_iv: float = 0.0) -> dict:
    """Alternate shape using CE/PE keys (second naming convention)."""
    return {
        "strikePrice": k,
        "CE": {"oi": call_oi, "iv": call_iv},
        "PE": {"oi": put_oi},
    }


# ── compute_pcr ──────────────────────────────────────────────────────────────

class TestComputePcr:
    def test_balanced(self):
        oc = [_strike(100, call_oi=1000, put_oi=1000)]
        assert compute_pcr(oc) == pytest.approx(1.0, rel=1e-3)

    def test_bearish_heavy_puts(self):
        oc = [_strike(100, call_oi=1000, put_oi=1500)]
        assert compute_pcr(oc) == pytest.approx(1.5, rel=1e-3)

    def test_bullish_heavy_calls(self):
        oc = [_strike(100, call_oi=2000, put_oi=800)]
        assert compute_pcr(oc) == pytest.approx(0.4, rel=1e-3)

    def test_multiple_strikes_summed(self):
        oc = [
            _strike(100, call_oi=500,  put_oi=300),
            _strike(200, call_oi=500,  put_oi=700),
        ]
        # total_call=1000, total_put=1000
        assert compute_pcr(oc) == pytest.approx(1.0, rel=1e-3)

    def test_ce_pe_shape(self):
        oc = [_strike_ce_pe(100, call_oi=1000, put_oi=1300)]
        assert compute_pcr(oc) == pytest.approx(1.3, rel=1e-3)

    def test_empty_list(self):
        assert compute_pcr([]) is None

    def test_zero_call_oi(self):
        oc = [_strike(100, call_oi=0, put_oi=500)]
        assert compute_pcr(oc) is None

    def test_missing_keys(self):
        # Rows without any OI fields should contribute 0 and not raise
        oc = [{"strike_price": 100}]
        # call_oi = 0 → None
        assert compute_pcr(oc) is None


# ── compute_max_pain ─────────────────────────────────────────────────────────

class TestComputeMaxPain:
    def test_single_strike(self):
        oc = [_strike(100, call_oi=1000, put_oi=1000)]
        assert compute_max_pain(oc) == 100.0

    def test_returns_min_pain_strike(self):
        # At strike 100: call buyers get max(0, 100-100)*OI + max(0, 100-100)*OI = 0
        # At strike 200: call buyers whose strike is ≤ 200 get (200-100)*1000 = 100000
        # So max pain should be the strike that minimises buyer payoffs
        oc = [
            _strike(100, call_oi=0,    put_oi=10_000),  # lots of puts
            _strike(200, call_oi=5000, put_oi=0),
        ]
        result = compute_max_pain(oc)
        # At candidate=100: pain = put pain 0 (k==candidate), call pain 0 → 0
        # At candidate=200: pain = call pain 0 + put pain from k=100 → (200-100)*10000 = 1_000_000
        # So min pain is at 100
        assert result == 100.0

    def test_empty_list(self):
        assert compute_max_pain([]) is None

    def test_invalid_strike_skipped(self):
        oc = [{"strike_price": "bad"}, _strike(100, call_oi=100, put_oi=100)]
        result = compute_max_pain(oc)
        assert result == 100.0

    def test_zero_strike_skipped(self):
        oc = [_strike(0, call_oi=100, put_oi=100), _strike(100, call_oi=100, put_oi=100)]
        result = compute_max_pain(oc)
        assert result == 100.0


# ── compute_atm_iv ───────────────────────────────────────────────────────────

class TestComputeAtmIv:
    def test_exact_match(self):
        oc = [_strike(100, call_iv=18.5)]
        assert compute_atm_iv(oc, spot=100.0) == pytest.approx(18.5)

    def test_nearest_strike_chosen(self):
        oc = [
            _strike(90,  call_iv=22.0),
            _strike(100, call_iv=18.0),
            _strike(110, call_iv=20.0),
        ]
        # spot=102 → nearest is 100
        assert compute_atm_iv(oc, spot=102.0) == pytest.approx(18.0)

    def test_ce_pe_shape(self):
        oc = [_strike_ce_pe(100, call_iv=15.0)]
        assert compute_atm_iv(oc, spot=100.0) == pytest.approx(15.0)

    def test_zero_spot(self):
        oc = [_strike(100, call_iv=20.0)]
        assert compute_atm_iv(oc, spot=0.0) is None

    def test_empty_list(self):
        assert compute_atm_iv([], spot=100.0) is None

    def test_zero_iv_strike_skipped(self):
        # Strike at 100 has IV=0 (missing) → should not be picked even if nearest
        oc = [
            _strike(100, call_iv=0.0),
            _strike(110, call_iv=19.0),
        ]
        assert compute_atm_iv(oc, spot=100.0) == pytest.approx(19.0)


# ── _pcr_to_sentiment ────────────────────────────────────────────────────────

class TestPcrSentiment:
    def test_bearish(self):
        assert _pcr_to_sentiment(1.4) == "BEARISH_OPTIONS"

    def test_bullish(self):
        assert _pcr_to_sentiment(0.6) == "BULLISH_OPTIONS"

    def test_neutral_high(self):
        assert _pcr_to_sentiment(1.0) == "NEUTRAL"

    def test_neutral_none(self):
        assert _pcr_to_sentiment(None) == "NEUTRAL"

    def test_boundary_bearish(self):
        # strictly > 1.3
        assert _pcr_to_sentiment(1.31) == "BEARISH_OPTIONS"
        assert _pcr_to_sentiment(1.3) == "NEUTRAL"

    def test_boundary_bullish(self):
        # strictly < 0.7
        assert _pcr_to_sentiment(0.69) == "BULLISH_OPTIONS"
        assert _pcr_to_sentiment(0.7) == "NEUTRAL"


# ── _nearest_expiry ──────────────────────────────────────────────────────────

class TestNearestExpiry:
    def test_returns_thursday(self):
        from datetime import date
        result = _nearest_expiry()
        d = date.fromisoformat(result)
        assert d.weekday() == 3, f"Expected Thursday (3), got weekday {d.weekday()} for {result}"

    def test_today_if_thursday(self, monkeypatch):
        from datetime import date
        import yukti.services.option_metrics_service as svc
        # Find the next Thursday
        today = date.today()
        days = (3 - today.weekday()) % 7
        a_thursday = today if days == 0 else today + __import__("datetime").timedelta(days=days)
        monkeypatch.setattr(svc, "date", type("_D", (), {"today": staticmethod(lambda: a_thursday)})())
        assert svc._nearest_expiry() == a_thursday.isoformat()
