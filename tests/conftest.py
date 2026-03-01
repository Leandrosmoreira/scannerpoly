"""
conftest.py — Fixtures e mocks compartilhados pelos testes.
"""

from datetime import datetime, timezone, timedelta
import pytest


def make_raw_market(
    market_id="mkt-001",
    question="Will X happen?",
    slug="will-x-happen",
    end_date: str | None = None,
    category="Sports",
    yes_token="tok-yes-001",
    no_token="tok-no-001",
    liquidity=5000.0,
    volume=12000.0,
    tags=None,
) -> dict:
    """Retorna um dict que imita a resposta da Gamma API."""
    if end_date is None:
        future = datetime.now(timezone.utc) + timedelta(minutes=30)
        end_date = future.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": market_id,
        "conditionId": f"0xcondition{market_id}",
        "question": question,
        "slug": slug,
        "endDate": end_date,
        "category": category,
        "tags": [{"label": "Soccer"}] if tags is None else tags,
        "active": True,
        "closed": False,
        "liquidity": liquidity,
        "volume": volume,
        "tokens": [
            {"token_id": yes_token, "outcome": "Yes"},
            {"token_id": no_token,  "outcome": "No"},
        ],
    }


@pytest.fixture
def raw_market():
    return make_raw_market()


@pytest.fixture
def raw_market_no_category():
    return make_raw_market(category="", tags=[{"label": "Tennis"}])


@pytest.fixture
def raw_market_no_tokens():
    m = make_raw_market()
    m["tokens"] = []
    return m


@pytest.fixture
def raw_market_no_enddate():
    m = make_raw_market()
    del m["endDate"]
    return m
