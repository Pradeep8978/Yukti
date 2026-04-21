"""tests/unit/test_indicators_daily.py — tests for timeframe param, ADX, daily S/R."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from yukti.signals.indicators import compute, IndicatorSnapshot


def _make_daily_ohlcv(n: int = 80, start: float = 1000.0, trend: float = 0.002) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    closes = start * np.cumprod(1 + trend + rng.normal(0, 0.01, n))
    highs = closes * (1 + abs(rng.normal(0, 0.008, n)))
    lows = closes * (1 - abs(rng.normal(0, 0.008, n)))
    opens = np.roll(closes, 1); opens[0] = start
    vols = rng.normal(5_000_000, 1_000_000, n).clip(1)
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols})
    df.index = pd.bdate_range("2024-01-02", periods=n)
    return df


class TestTimeframeParam:
    def test_compute_accepts_timeframe_5m(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="5m")
        assert isinstance(snap, IndicatorSnapshot)

    def test_compute_accepts_timeframe_daily(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="daily")
        assert isinstance(snap, IndicatorSnapshot)

    def test_daily_timeframe_has_adx(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="daily")
        assert hasattr(snap, "adx")
        assert snap.adx is not None
        assert 0 <= snap.adx <= 100

    def test_5m_timeframe_adx_is_none(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="5m")
        assert snap.adx is None

    def test_daily_timeframe_has_support_resistance(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="daily")
        assert hasattr(snap, "daily_support")
        assert hasattr(snap, "daily_resistance")
        assert snap.daily_support is not None
        assert snap.daily_resistance is not None
        assert snap.daily_support < snap.daily_resistance

    def test_5m_timeframe_sr_is_none(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="5m")
        assert snap.daily_support is None
        assert snap.daily_resistance is None
