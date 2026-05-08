"""tests/unit/test_catalyst_service.py — catalyst classifier + scorer."""
from __future__ import annotations

import pytest

from yukti.services.news_provider import (
    NewsItem,
    classify_headline,
    get_news_provider,
    NSEAnnouncementsProvider,
    MarketauxNewsProvider,
)
from yukti.services.catalyst_service import (
    score_catalyst_for_symbol,
    _score_items,
)


class TestClassifier:
    def test_results_categorized(self):
        assert classify_headline("Q3 FY26 results — revenue up 18%") == "RESULTS"
        assert classify_headline("Quarterly Earnings Release") == "RESULTS"

    def test_mna_categorized(self):
        assert classify_headline("Acquisition of Foo Pvt Ltd") == "M&A"
        assert classify_headline("Open offer to public shareholders") == "M&A"
        assert classify_headline("Demerger of consumer business") == "M&A"

    def test_guidance_categorized(self):
        assert classify_headline("Order win worth Rs 1,200 cr from MoD") == "GUIDANCE"
        assert classify_headline("Capex announcement: greenfield plant") == "GUIDANCE"

    def test_board_categorized(self):
        assert classify_headline("Board meeting on 12-Apr-2026") == "BOARD"
        assert classify_headline("Buyback of equity shares") == "BOARD"

    def test_unknown_falls_to_other(self):
        assert classify_headline("Random update") == "OTHER"
        assert classify_headline("") == "OTHER"


class TestScoring:
    def test_hard_catalyst_eight_points(self):
        items = [NewsItem("RELIANCE", "2026-01-01T00:00:00", "M&A", "Acquisition of X")]
        assert _score_items(items) == 8

    def test_soft_catalyst_three_points(self):
        items = [NewsItem("ITC", "2026-01-01T00:00:00", "BOARD", "Dividend record date")]
        assert _score_items(items) == 3

    def test_hard_plus_soft_capped_at_eleven_then_total_capped_at_fifteen(self):
        items = [
            NewsItem("X", "t", "M&A", "Acquisition"),
            NewsItem("X", "t", "BOARD", "Buyback"),
        ]
        assert _score_items(items) == 11   # 8 + 3
        # +4 for results window pushes to 15 (capped)
        assert score_catalyst_for_symbol("X", items, in_results_window=True) == 15

    def test_results_window_alone_four_points(self):
        assert score_catalyst_for_symbol("X", [], in_results_window=True) == 4

    def test_no_input_zero(self):
        assert score_catalyst_for_symbol("X", [], in_results_window=False) == 0


class TestProviderFactory:
    def test_default_is_nse(self):
        provider = get_news_provider("nse_only")
        assert isinstance(provider, NSEAnnouncementsProvider)

    def test_unknown_falls_back_to_nse(self):
        provider = get_news_provider("does_not_exist")
        assert isinstance(provider, NSEAnnouncementsProvider)

    def test_marketaux_no_key_is_no_op(self):
        # No key → factory still constructs, fetch returns empty list.
        import asyncio
        provider = get_news_provider("marketaux", api_key="")
        assert isinstance(provider, MarketauxNewsProvider)
        items = asyncio.get_event_loop().run_until_complete(provider.fetch_recent(24))
        assert items == []
