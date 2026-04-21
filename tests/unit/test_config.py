"""tests/unit/test_config.py — verify new config fields exist with correct defaults."""
from __future__ import annotations

import pytest
from yukti.config import Settings


class TestScannerConfig:
    def test_scanner_pick_count_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.scanner_pick_count == 15

    def test_min_turnover_cr_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.min_turnover_cr == 10

    def test_volume_surge_threshold_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.volume_surge_threshold == 2.0

    def test_price_move_threshold_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.price_move_threshold == 1.5

    def test_intraday_refresh_times_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.intraday_refresh_times == ["10:00", "12:00"]


class TestDailyCandleConfig:
    def test_daily_candle_history_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.daily_candle_history == 60

    def test_daily_cache_ttl_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.daily_cache_ttl == 3600 * 8
