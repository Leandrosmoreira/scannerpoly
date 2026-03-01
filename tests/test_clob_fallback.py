"""
test_clob_fallback.py — Testa a cadeia de fallback de pricing do ClobClient.
Usa unittest.mock para evitar chamadas reais à API.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from clob_client import ClobClient
from models import MarketMeta
import config


def make_meta(yes_token="tok-yes", no_token="tok-no") -> MarketMeta:
    return MarketMeta(
        market_id="mkt-001",
        condition_id="0xcond001",
        question="Test market?",
        slug="test-market",
        url="https://polymarket.com/event/test-market",
        category="Test",
        tags=[],
        end_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        yes_token_id=yes_token,
        no_token_id=no_token,
        liquidity=1000.0,
        volume=5000.0,
    )


class TestPricingFallback:
    def setup_method(self):
        self.client = ClobClient()

    def test_uses_mid_when_available(self):
        markets = [make_meta()]
        with patch.object(self.client, "get_midpoints_bulk", return_value={"tok-yes": 0.92, "tok-no": 0.08}):
            with patch.object(self.client, "get_last_trades_bulk", return_value={}):
                quotes = self.client.fetch_quotes(markets)
        q = quotes["mkt-001"]
        assert q.yes_price == pytest.approx(0.92)
        assert q.no_price == pytest.approx(0.08)
        assert q.price_source == "mid"
        assert q.has_liquidity is True

    def test_falls_back_to_last_trade_when_no_mid(self):
        markets = [make_meta()]
        with patch.object(self.client, "get_midpoints_bulk", return_value={}):
            with patch.object(self.client, "get_last_trades_bulk", return_value={"tok-yes": 0.88, "tok-no": 0.13}):
                with patch.object(self.client, "_fetch_individual_parallel", return_value={}):
                    quotes = self.client.fetch_quotes(markets)
        q = quotes["mkt-001"]
        assert q.yes_price == pytest.approx(0.88)
        assert q.price_source == "last_trade"

    def test_falls_back_to_price_endpoint(self):
        markets = [make_meta()]
        with patch.object(self.client, "get_midpoints_bulk", return_value={}):
            with patch.object(self.client, "get_last_trades_bulk", return_value={}):
                with patch.object(
                    self.client, "_fetch_individual_parallel",
                    return_value={"tok-yes": (0.85, "price_ep"), "tok-no": (0.16, "price_ep")}
                ):
                    quotes = self.client.fetch_quotes(markets)
        q = quotes["mkt-001"]
        assert q.yes_price == pytest.approx(0.85)
        assert q.price_source == "price_ep"

    def test_returns_none_price_on_all_failure(self):
        markets = [make_meta()]
        with patch.object(self.client, "get_midpoints_bulk", return_value={}):
            with patch.object(self.client, "get_last_trades_bulk", return_value={}):
                with patch.object(self.client, "_fetch_individual_parallel", return_value={}):
                    quotes = self.client.fetch_quotes(markets)
        q = quotes["mkt-001"]
        assert q.yes_price is None
        assert q.no_price is None
        assert q.price_source == "none"
        assert q.has_liquidity is False

    def test_spread_threshold_triggers_last_trade_preference(self):
        """Se mid dá spread aberto, deve preferir last_trade."""
        markets = [make_meta()]
        # mid: yes=0.70, no=0.50 → spread = 0.20 > SPREAD_THRESHOLD(0.15)
        # last: yes=0.68, no=0.33
        with patch.object(self.client, "get_midpoints_bulk",
                          return_value={"tok-yes": 0.70, "tok-no": 0.50}):
            with patch.object(self.client, "get_last_trades_bulk",
                              return_value={"tok-yes": 0.68, "tok-no": 0.33}):
                quotes = self.client.fetch_quotes(markets)
        q = quotes["mkt-001"]
        # Com spread aberto, deve usar last_trade
        assert q.price_source == "last_trade"

    def test_empty_market_list_returns_empty(self):
        quotes = self.client.fetch_quotes([])
        assert quotes == {}

    def test_mid_from_book_calculates_correctly(self):
        book = {
            "bids": [{"price": "0.90"}, {"price": "0.88"}],
            "asks": [{"price": "0.94"}, {"price": "0.96"}],
        }
        mid = ClobClient._mid_from_book(book)
        assert mid == pytest.approx(0.92)  # (0.90 + 0.94) / 2

    def test_mid_from_book_empty_asks(self):
        book = {"bids": [{"price": "0.90"}], "asks": []}
        mid = ClobClient._mid_from_book(book)
        assert mid == pytest.approx(0.90)

    def test_mid_from_book_empty_returns_none(self):
        mid = ClobClient._mid_from_book({"bids": [], "asks": []})
        assert mid is None
