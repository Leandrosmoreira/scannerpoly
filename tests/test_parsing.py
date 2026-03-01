"""
test_parsing.py — Testa parsing de datas, extração de tokens e construção de URLs.
"""

from datetime import datetime, timezone, timedelta

import pytest

from gamma_client import GammaClient
from tests.conftest import make_raw_market

client = GammaClient()


# ── Parsing de endDate ─────────────────────────────────────────────────────────

class TestParseEndDate:
    def test_iso8601_utc(self):
        dt = GammaClient._parse_dt("2024-01-15T15:00:00Z")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2024 and dt.month == 1 and dt.day == 15

    def test_iso8601_with_negative_offset(self):
        dt = GammaClient._parse_dt("2024-01-15T12:00:00-03:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 15  # 12 + 3

    def test_iso8601_with_positive_offset(self):
        dt = GammaClient._parse_dt("2024-01-15T17:00:00+02:00")
        assert dt is not None
        assert dt.hour == 15  # 17 - 2

    def test_naive_datetime_assumed_utc(self):
        dt = GammaClient._parse_dt("2024-01-15T15:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_invalid_returns_none(self):
        dt = GammaClient._parse_dt("not-a-date")
        assert dt is None

    def test_empty_returns_none(self):
        dt = GammaClient._parse_dt("")
        assert dt is None


# ── Extração de token IDs ──────────────────────────────────────────────────────

class TestTokenExtraction:
    def test_extracts_yes_token(self):
        tokens = [
            {"token_id": "abc", "outcome": "Yes"},
            {"token_id": "def", "outcome": "No"},
        ]
        assert GammaClient._extract_token(tokens, "Yes") == "abc"

    def test_extracts_no_token(self):
        tokens = [
            {"token_id": "abc", "outcome": "Yes"},
            {"token_id": "def", "outcome": "No"},
        ]
        assert GammaClient._extract_token(tokens, "No") == "def"

    def test_case_insensitive(self):
        tokens = [{"token_id": "abc", "outcome": "YES"}]
        assert GammaClient._extract_token(tokens, "Yes") == "abc"

    def test_missing_outcome_returns_none(self):
        tokens = [{"token_id": "abc", "outcome": "Yes"}]
        assert GammaClient._extract_token(tokens, "No") is None

    def test_empty_list_returns_none(self):
        assert GammaClient._extract_token([], "Yes") is None


# ── parse_market completo ──────────────────────────────────────────────────────

class TestParseMarket:
    def test_valid_market(self, raw_market):
        m = client._parse_market(raw_market)
        assert m is not None
        assert m.market_id == "mkt-001"
        assert m.yes_token_id == "tok-yes-001"
        assert m.no_token_id == "tok-no-001"
        assert m.category == "Sports"
        assert m.end_date.tzinfo == timezone.utc

    def test_url_construction(self, raw_market):
        m = client._parse_market(raw_market)
        assert m is not None
        assert m.url == "https://polymarket.com/event/will-x-happen"

    def test_no_tokens_returns_none(self, raw_market_no_tokens):
        assert client._parse_market(raw_market_no_tokens) is None

    def test_no_enddate_returns_none(self, raw_market_no_enddate):
        assert client._parse_market(raw_market_no_enddate) is None

    def test_category_fallback_to_tag(self, raw_market_no_category):
        m = client._parse_market(raw_market_no_category)
        assert m is not None
        assert m.category == "Tennis"

    def test_category_fallback_to_other(self):
        raw = make_raw_market(category="", tags=[])
        m = client._parse_market(raw)
        assert m is not None
        assert m.category == "Other"

    def test_liquidity_defaults_to_zero(self):
        raw = make_raw_market()
        del raw["liquidity"]
        m = client._parse_market(raw)
        assert m is not None
        assert m.liquidity == 0.0
