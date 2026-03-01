"""
models.py — Dataclasses que representam os dados do scanner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MarketMeta:
    """Metadata de um mercado vindo da Gamma API."""
    market_id: str          # campo "id" do Gamma
    condition_id: str       # campo "conditionId"
    question: str
    slug: str
    url: str                # https://polymarket.com/event/{slug}
    category: str           # "Other" se ausente
    tags: list[str]
    end_date: datetime      # UTC
    yes_token_id: str
    no_token_id: str
    liquidity: float = 0.0
    volume: float = 0.0


@dataclass
class MarketQuote:
    """Cotação atual de um mercado (YES/NO)."""
    yes_price: float | None = None   # preço final exibido
    no_price: float | None = None
    yes_mid: float | None = None     # raw do midpoint
    no_mid: float | None = None
    yes_last: float | None = None    # raw do last trade
    no_last: float | None = None
    spread: float | None = None      # yes_price + no_price - 1.0
    price_source: str = "none"       # "mid" | "last_trade" | "price_ep" | "book" | "none"
    has_liquidity: bool = False
    fetched_at: datetime | None = None


@dataclass
class MarketRow:
    """Market completo: metadata + cotação + campos computados."""
    meta: MarketMeta
    quote: MarketQuote
    time_to_end_sec: int             # segundos até endDate
    is_new: bool = False             # entrou neste ciclo
    price_delta_yes: float | None = None   # vs ciclo anterior
    price_delta_no: float | None = None


@dataclass
class ScanResult:
    """Resultado completo de um ciclo de scan."""
    scan_ts: datetime
    cycle_num: int
    window_minutes: int
    markets: list[MarketRow] = field(default_factory=list)
    by_category: dict[str, list[MarketRow]] = field(default_factory=dict)
    elapsed_sec: float = 0.0
    new_count: int = 0
    dropped_count: int = 0
