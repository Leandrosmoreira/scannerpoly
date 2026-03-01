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
    neg_risk: bool = False       # True se multi-outcome (>2 outcomes)
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


# ══════════════════════════════════════════════════════════════════════════════
# ── Lending Bot ──────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BookAnalysis:
    """Resultado da analise do order book de um token."""
    token_id: str
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    depth_bid_usd: float = 0.0
    depth_ask_usd: float = 0.0
    slippage_100: float = 0.0       # slippage para comprar $100
    slippage_500: float = 0.0       # slippage para comprar $500
    has_wall: bool = False           # ordem > 5x mediana detectada
    estimated_fill_price: float | None = None
    levels_bid: int = 0
    levels_ask: int = 0
    is_tradeable: bool = False       # profundidade e spread OK


@dataclass
class LendingSignal:
    """Sinal de entrada detectado pelo filtro de lending."""
    market_id: str
    condition_id: str
    question: str
    slug: str
    url: str
    token_id: str               # token do lado vencedor (YES ou NO)
    side: str                   # "YES" ou "NO"
    probability: float          # preco do lado forte (ex: 0.99)
    opposite_prob: float        # preco do lado fraco (ex: 0.01)
    spread: float | None        # spread do book
    book_depth_usd: float       # profundidade do lado da compra (asks)
    book: BookAnalysis | None   # analise completa do book
    time_to_end_sec: int        # segundos ate endDate
    expected_roi: float         # (1 - prob) / prob
    annualized_apy: float       # ROI anualizado com base no tempo
    score: float                # score composto para ranking
    category: str
    neg_risk: bool = False          # True se multi-outcome market
    detected_at: datetime | None = None


@dataclass
class BotPosition:
    """Posicao aberta pelo bot (ordem colocada no CLOB)."""
    position_id: str            # UUID interno
    market_id: str
    condition_id: str
    question: str
    token_id: str
    side: str                   # "YES" ou "NO"
    entry_price: float          # preco limite da ordem
    size_shares: float          # shares ordenados
    cost_usd: float             # entry_price * size_shares
    order_id: str               # ID da ordem no CLOB (ou "DRY_..." em dry-run)
    order_status: str           # "pending" | "filled" | "partial" | "cancelled" | "failed"
    fill_price: float | None = None
    fill_size: float | None = None
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expected_resolve_at: datetime | None = None
    resolved: bool = False
    won: bool | None = None     # True=ganhou, False=perdeu, None=pendente
    payout: float = 0.0         # $1.00 * fill_size se ganhou, 0 se perdeu
    pnl: float | None = None    # payout - cost_usd
