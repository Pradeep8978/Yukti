"""
tests/unit/test_backtest_engine.py
Regression tests for the backtest engine's EOD squareoff path.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from yukti.backtest import BacktestEngine, SimPosition


def _df(base: float, dates: pd.DatetimeIndex, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = base + np.cumsum(rng.normal(0.1, 0.5, len(dates)))
    return pd.DataFrame(
        {
            "open": c,
            "high": c + 0.3,
            "low": c - 0.3,
            "close": c,
            "volume": rng.integers(100_000, 500_000, len(dates)).astype(float),
        },
        index=dates,
    ).astype(float)


@pytest.fixture
def engine_with_gap():
    """Engine where symbol 'A' has a Friday data gap while 'B' / NIFTY trade."""
    dates = pd.bdate_range(start="2024-01-01", periods=20)
    a = _df(100, dates, seed=1)
    b = _df(200, dates, seed=2)
    nifty = _df(22000, dates, seed=3)
    missing_friday = [d for d in dates if d.dayofweek == 4][1]
    a = a.drop(missing_friday)
    engine = BacktestEngine(
        candles={"A": a, "B": b, "NIFTY": nifty},
        nifty_candles=nifty,
        account_value=500_000.0,
    )
    return engine, missing_friday


def _open_long(engine: BacktestEngine, sym: str, entry: float, ts) -> None:
    engine.broker.positions[sym] = SimPosition(
        symbol=sym,
        direction="LONG",
        quantity=100,
        entry_price=entry,
        stop_loss=entry * 0.97,
        target_1=entry * 1.05,
        target_2=entry * 1.08,
        holding="intraday",
        entry_time=ts,
    )


class TestSquareoffPriceFallback:
    """Squareoff must never use 0.0 as the exit price when the symbol's bar
    is missing — that registers a fake -100% loss and silently wipes the
    account in any backtest where one symbol has a holiday or halt the
    others don't."""

    def test_falls_back_to_brokers_last_known_close(self, engine_with_gap):
        engine, friday = engine_with_gap
        thursday = friday - pd.Timedelta(days=1)
        entry = float(engine.candles["A"].loc[thursday, "close"])
        _open_long(engine, "A", entry, thursday.to_pydatetime())

        # Prior bar updates broker's cache with Thursday's close
        engine.broker.update_prices(thursday, {"A": entry, "B": 300.0}, {}, {})

        # Today's prices dict is missing 'A' (no candle on this Friday)
        engine._squareoff("A", {"B": 305.0}, friday)

        closed = engine.broker.closed_trades[0]
        assert closed.exit_price == entry, "should reuse last-known close, not 0.0"
        assert closed.pnl == 0.0

    def test_falls_back_to_entry_when_no_cache(self, engine_with_gap, caplog):
        engine, friday = engine_with_gap
        thursday = friday - pd.Timedelta(days=1)
        entry = float(engine.candles["A"].loc[thursday, "close"])
        _open_long(engine, "A", entry, thursday.to_pydatetime())

        with caplog.at_level("WARNING", logger="yukti.backtest"):
            engine._squareoff("A", {"B": 305.0}, friday)

        closed = engine.broker.closed_trades[0]
        assert closed.exit_price == entry
        assert closed.pnl == 0.0
        assert any("no price available" in r.message for r in caplog.records)

    def test_uses_todays_close_when_present(self, engine_with_gap):
        engine, friday = engine_with_gap
        thursday = friday - pd.Timedelta(days=1)
        entry = float(engine.candles["A"].loc[thursday, "close"])
        _open_long(engine, "A", entry, thursday.to_pydatetime())

        engine._squareoff("A", {"A": 105.5}, friday)

        closed = engine.broker.closed_trades[0]
        assert closed.exit_price == 105.5
        assert closed.pnl == pytest.approx((105.5 - entry) * 100)
