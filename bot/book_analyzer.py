"""
book_analyzer.py — Analise de profundidade do order book para o lending bot.
Busca GET /book para um token e calcula metricas de liquidez e slippage.
"""

from __future__ import annotations

import logging
import statistics

import requests

import config
from models import BookAnalysis

log = logging.getLogger(__name__)


class BookAnalyzer:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def analyze(self, token_id: str) -> BookAnalysis:
        """
        Busca o order book e retorna BookAnalysis com metricas.
        Para o lending bot, queremos COMPRAR shares — entao olhamos os ASKS
        (quem esta vendendo) para calcular depth e slippage.
        """
        book = self._fetch_book(token_id)
        if book is None:
            return BookAnalysis(token_id=token_id)

        bids_raw = book.get("bids") or []
        asks_raw = book.get("asks") or []

        # Parsear e ordenar
        bids = self._parse_levels(bids_raw, reverse=True)    # maior preco primeiro
        asks = self._parse_levels(asks_raw, reverse=False)    # menor preco primeiro

        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None

        spread: float | None = None
        if best_bid is not None and best_ask is not None:
            spread = round(best_ask - best_bid, 6)

        # Profundidade total em USD
        depth_bid_usd = sum(p * s for p, s in bids)
        depth_ask_usd = sum(p * s for p, s in asks)

        # Slippage estimado (quanto mais caro fica ao comprar $X)
        slippage_100 = self._calc_slippage(asks, 100.0, best_ask)
        slippage_500 = self._calc_slippage(asks, 500.0, best_ask)

        # Wall detection: alguma ordem > 5x a mediana?
        has_wall = False
        if len(asks) >= 3:
            sizes = [s for _, s in asks]
            median_size = statistics.median(sizes)
            if median_size > 0:
                has_wall = any(s > median_size * 5 for s in sizes)

        # Fill price estimado para $100
        estimated_fill = self._estimated_fill_price(asks, config.BOT_MAX_POSITION_USD)

        # Tradeable?
        is_tradeable = (
            depth_ask_usd >= config.BOT_MIN_BOOK_DEPTH_USD
            and spread is not None
            and spread <= config.BOT_MAX_BOOK_SPREAD
            and best_ask is not None
        )

        return BookAnalysis(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            depth_bid_usd=round(depth_bid_usd, 2),
            depth_ask_usd=round(depth_ask_usd, 2),
            slippage_100=round(slippage_100, 6),
            slippage_500=round(slippage_500, 6),
            has_wall=has_wall,
            estimated_fill_price=estimated_fill,
            levels_bid=len(bids),
            levels_ask=len(asks),
            is_tradeable=is_tradeable,
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _fetch_book(self, token_id: str) -> dict | None:
        try:
            resp = self._session.get(
                config.CLOB_BASE + "/book",
                params={"token_id": token_id},
                timeout=config.REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.debug("Book fetch falhou para %s: %s", token_id[:16], exc)
            return None

    @staticmethod
    def _parse_levels(raw: list, reverse: bool) -> list[tuple[float, float]]:
        """Parseia [{price, size}] e ordena. Retorna [(price, size)]."""
        levels: list[tuple[float, float]] = []
        for item in raw:
            try:
                p = float(item.get("price", 0))
                s = float(item.get("size", 0))
                if p > 0 and s > 0:
                    levels.append((p, s))
            except (TypeError, ValueError):
                continue
        levels.sort(key=lambda x: x[0], reverse=reverse)
        return levels

    @staticmethod
    def _calc_slippage(
        asks: list[tuple[float, float]], target_usd: float, best_ask: float | None
    ) -> float:
        """
        Calcula slippage para comprar $target_usd no book.
        Slippage = (preco medio ponderado - best_ask) / best_ask.
        """
        if not asks or best_ask is None or best_ask <= 0:
            return 0.0

        remaining = target_usd
        total_cost = 0.0
        total_shares = 0.0

        for price, size in asks:
            level_usd = price * size
            if level_usd >= remaining:
                shares = remaining / price
                total_cost += remaining
                total_shares += shares
                remaining = 0
                break
            else:
                total_cost += level_usd
                total_shares += size
                remaining -= level_usd

        if total_shares == 0 or remaining > 0:
            return 999.0  # liquidez insuficiente

        avg_price = total_cost / total_shares
        return (avg_price - best_ask) / best_ask

    @staticmethod
    def _estimated_fill_price(
        asks: list[tuple[float, float]], target_usd: float
    ) -> float | None:
        """Preco medio ponderado para comprar $target_usd."""
        if not asks:
            return None

        remaining = target_usd
        total_cost = 0.0
        total_shares = 0.0

        for price, size in asks:
            level_usd = price * size
            if level_usd >= remaining:
                shares = remaining / price
                total_cost += remaining
                total_shares += shares
                break
            else:
                total_cost += level_usd
                total_shares += size
                remaining -= level_usd

        if total_shares == 0:
            return None
        return round(total_cost / total_shares, 6)
