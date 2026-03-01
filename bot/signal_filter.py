"""
signal_filter.py — Filtro de sinais para o lending bot.

Pipeline de 5 filtros em cascata:
1. Probabilidade >= BOT_MIN_PROBABILITY
2. Tempo ate resolucao <= BOT_MAX_MINUTES_TO_END
3. Liquidez do book (profundidade e spread)
4. Limites de risco (exposure, posicoes)
5. Deduplicacao (nao entrar 2x no mesmo market)

Saida: lista de LendingSignal ordenada por score (melhor primeiro).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from models import BookAnalysis, LendingSignal, MarketRow, ScanResult
from bot.book_analyzer import BookAnalyzer

log = logging.getLogger(__name__)


class SignalFilter:
    def __init__(self, book_analyzer: BookAnalyzer) -> None:
        self._book = book_analyzer
        self._active_market_ids: set[str] = set()

    def set_active_positions(self, market_ids: set[str]) -> None:
        """Atualiza IDs de mercados com posicao aberta (para dedup)."""
        self._active_market_ids = market_ids

    def filter(self, result: ScanResult) -> list[LendingSignal]:
        """
        Aplica todos os filtros e retorna sinais ordenados por score.
        """
        candidates: list[LendingSignal] = []
        now = datetime.now(timezone.utc)

        for row in result.markets:
            signal = self._evaluate(row, now)
            if signal is not None:
                candidates.append(signal)

        # Ordenar por score (maior = melhor)
        candidates.sort(key=lambda s: s.score, reverse=True)

        if candidates:
            log.info(
                "Sinais: %d encontrados (melhor: %.1f%% %s em %s)",
                len(candidates),
                candidates[0].probability * 100,
                candidates[0].side,
                candidates[0].question[:40],
            )

        return candidates

    def _evaluate(self, row: MarketRow, now: datetime) -> LendingSignal | None:
        """Avalia um MarketRow e retorna LendingSignal se passar em todos os filtros."""

        # ── Filtro 1: Probabilidade ──────────────────────────────────────────
        yes_p = row.quote.yes_price
        no_p = row.quote.no_price

        if yes_p is None or no_p is None:
            return None
        if not row.quote.has_liquidity:
            return None

        # Qual lado eh o forte?
        if yes_p >= config.BOT_MIN_PROBABILITY:
            side = "YES"
            probability = yes_p
            opposite = no_p
            token_id = row.meta.yes_token_id
        elif no_p >= config.BOT_MIN_PROBABILITY:
            side = "NO"
            probability = no_p
            opposite = yes_p
            token_id = row.meta.no_token_id
        else:
            return None  # nenhum lado acima do minimo

        # ── Filtro 2: Tempo ──────────────────────────────────────────────────
        eta = row.time_to_end_sec
        if eta <= 0:
            return None  # ja passou
        if eta > config.BOT_MAX_MINUTES_TO_END * 60:
            return None  # muito longe

        # ── Filtro 3: Deduplicacao ───────────────────────────────────────────
        if row.meta.market_id in self._active_market_ids:
            return None

        # ── Filtro 4: Book analysis (somente para candidatos validos) ────────
        book = self._book.analyze(token_id)
        if not book.is_tradeable:
            log.debug(
                "Skip %s: book nao tradeable (depth=$%.0f spread=%.4f)",
                row.meta.question[:30],
                book.depth_ask_usd,
                book.spread or 0,
            )
            return None

        # ── Calcular metricas ────────────────────────────────────────────────
        roi = (1.0 - probability) / probability if probability > 0 else 0
        minutes = max(eta / 60.0, 1.0)
        annualized = roi * (365 * 24 * 60 / minutes) if minutes > 0 else 0

        # ── Score ────────────────────────────────────────────────────────────
        score = self._compute_score(probability, eta, book)

        return LendingSignal(
            market_id=row.meta.market_id,
            condition_id=row.meta.condition_id,
            question=row.meta.question,
            slug=row.meta.slug,
            url=row.meta.url,
            token_id=token_id,
            side=side,
            probability=probability,
            opposite_prob=opposite,
            spread=book.spread,
            book_depth_usd=book.depth_ask_usd,
            book=book,
            time_to_end_sec=eta,
            expected_roi=round(roi, 6),
            annualized_apy=round(annualized, 2),
            score=round(score, 4),
            category=row.meta.category,
            detected_at=now,
        )

    @staticmethod
    def _compute_score(probability: float, eta_sec: int, book: BookAnalysis) -> float:
        """
        Score composto: quanto maior, melhor o sinal.
        Pesos configuraveis em config.BOT_W_*.
        """
        # Normalize probabilidade: 0.97 → 0.0, 1.0 → 1.0
        prob_norm = _normalize(probability, config.BOT_MIN_PROBABILITY, 1.0)

        # Normalize tempo: menos tempo = melhor. 60min → 0.0, 1min → 1.0
        max_sec = config.BOT_MAX_MINUTES_TO_END * 60
        time_norm = _normalize(max_sec - eta_sec, 0, max_sec)

        # Normalize depth: $500 → 0.0, $10000 → 1.0
        depth_norm = _normalize(book.depth_ask_usd, config.BOT_MIN_BOOK_DEPTH_USD, 10000)

        # Normalize spread: 0.03 → 0.0, 0.0 → 1.0 (invertido)
        spread_val = book.spread if book.spread is not None else config.BOT_MAX_BOOK_SPREAD
        spread_norm = _normalize(config.BOT_MAX_BOOK_SPREAD - spread_val, 0, config.BOT_MAX_BOOK_SPREAD)

        return (
            config.BOT_W_PROB * prob_norm
            + config.BOT_W_TIME * time_norm
            + config.BOT_W_DEPTH * depth_norm
            + config.BOT_W_SPREAD * spread_norm
        )


def _normalize(value: float, low: float, high: float) -> float:
    """Clamp e normaliza para [0, 1]."""
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))
