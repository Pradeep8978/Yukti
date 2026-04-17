"""
tests/conftest.py
Shared pytest fixtures for Yukti tests.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def sample_ohlcv(n: int = 80) -> pd.DataFrame:
    """Returns a synthetic OHLCV DataFrame suitable for indicator testing."""
    rng    = np.random.default_rng(0)
    closes = 1000 * np.cumprod(1 + rng.normal(0.001, 0.005, n))
    highs  = closes * (1 + abs(rng.normal(0, 0.003, n)))
    lows   = closes * (1 - abs(rng.normal(0, 0.003, n)))
    opens  = np.roll(closes, 1); opens[0] = 1000.0
    vols   = rng.normal(500_000, 100_000, n).clip(1)

    df = pd.DataFrame({
        "open":   opens, "high": highs,
        "low":    lows,  "close": closes, "volume": vols,
    })
    df.index = pd.date_range("2024-01-02 09:15", periods=n, freq="5min")
    return df


@pytest.fixture
def sample_snap(sample_ohlcv):
    from yukti.signals.indicators import compute
    return compute(sample_ohlcv)
