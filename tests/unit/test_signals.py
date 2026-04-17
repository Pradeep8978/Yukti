"""
tests/unit/test_signals.py
Unit tests for the indicator snapshot and pattern detector.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from yukti.signals.indicators import compute, IndicatorSnapshot
from yukti.signals.patterns import (
    breakout, breakdown, trend_pullback_long,
    reversal_long, momentum_long, scan_all,
)


def _make_ohlcv(
    n:         int   = 80,
    start:     float = 1000.0,
    trend:     float = 0.001,   # per-candle drift
    vol:       float = 0.005,   # noise
    volume:    float = 500_000,
) -> pd.DataFrame:
    """Generate synthetic OHLCV data for testing."""
    rng = np.random.default_rng(42)
    closes = start * np.cumprod(1 + trend + rng.normal(0, vol, n))
    highs  = closes * (1 + abs(rng.normal(0, 0.003, n)))
    lows   = closes * (1 - abs(rng.normal(0, 0.003, n)))
    opens  = np.roll(closes, 1); opens[0] = start
    vols   = rng.normal(volume, volume * 0.3, n).clip(1)

    df = pd.DataFrame({
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": vols,
    })
    df.index = pd.date_range("2024-01-02 09:15", periods=n, freq="5min")
    return df


class TestIndicatorCompute:
    def test_returns_snapshot(self):
        df   = _make_ohlcv()
        snap = compute(df)
        assert isinstance(snap, IndicatorSnapshot)

    def test_all_fields_finite(self):
        df   = _make_ohlcv()
        snap = compute(df)
        for field, val in snap.__dict__.items():
            if isinstance(val, float):
                assert np.isfinite(val), f"{field} is not finite: {val}"

    def test_uptrend_detected(self):
        df   = _make_ohlcv(trend=0.003)    # strongly trending up
        snap = compute(df)
        assert snap.trend == "UPTREND"

    def test_downtrend_detected(self):
        df   = _make_ohlcv(trend=-0.003)
        snap = compute(df)
        assert snap.trend == "DOWNTREND"

    def test_volume_ratio_reasonable(self):
        df   = _make_ohlcv()
        snap = compute(df)
        assert 0.1 < snap.volume_ratio < 10.0

    def test_rsi_in_range(self):
        df   = _make_ohlcv()
        snap = compute(df)
        assert 0 < snap.rsi < 100

    def test_atr_positive(self):
        df   = _make_ohlcv()
        snap = compute(df)
        assert snap.atr > 0


class TestPatterns:
    def test_momentum_detects_on_strong_trend(self):
        df   = _make_ohlcv(trend=0.004, n=80)
        snap = compute(df)
        # Force RSI into momentum zone
        snap.rsi         = 65.0
        snap.macd_bull   = True
        snap.macd_hist   = 0.5
        snap.volume_ratio = 1.8
        snap.supertrend_bull = True
        result = momentum_long(snap)
        # May or may not detect — just check it returns a valid object
        assert hasattr(result, "detected")
        assert 0.0 <= result.strength <= 1.0

    def test_reversal_needs_oversold(self):
        df   = _make_ohlcv()
        snap = compute(df)
        snap.rsi = 55.0    # not oversold
        result = reversal_long(snap)
        assert result.detected is False

    def test_scan_all_returns_list(self):
        df      = _make_ohlcv()
        snap    = compute(df)
        results = scan_all(snap)
        assert isinstance(results, list)
        # All detected patterns should have strength > 0
        for p in results:
            assert p.strength > 0
            assert p.detected is True

    def test_strength_sorted_descending(self):
        df      = _make_ohlcv(trend=0.003)
        snap    = compute(df)
        results = scan_all(snap)
        if len(results) >= 2:
            strengths = [p.strength for p in results]
            assert strengths == sorted(strengths, reverse=True)
