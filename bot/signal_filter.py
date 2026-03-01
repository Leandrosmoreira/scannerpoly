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

        # Contadores de rejeicao para debug
        rej = {"no_price": 0, "no_liq": 0, "low_prob": 0, "eta": 0, "dedup": 0, "book": 0}

        for row in result.markets:
            signal = self._evaluate(row, now, rej)
            if signal is not None:
                candidates.append(signal)

        # Ordenar por score (maior = melhor)
        candidates.sort(key=lambda s: s.score, reverse=True)

        # Log resumo de rejeicoes
        total_rej = sum(rej.values())
        if total_rej > 0 or candidates:
            log.info(
                "Filtro: %d mercados → %d sinais | rejeitados: prob=%d eta=%d book=%d liq=%d dedup=%d price=%d",
                len(result.markets), len(candidates),
                rej["low_prob"], rej["eta"], rej["book"],
                rej["no_liq"], rej["dedup"], rej["no_price"],
            )

        if candidates:
            log.info(
                "Sinais: %d encontrados (melhor: %.1f%% %s em %s)",
                len(candidates),
                candidates[0].probability * 100,
                candidates[0].side,
                candidates[0].question[:40],
            )

        return candidates

    def _evaluate(self, row: MarketRow, now: datetime, rej: dict) -> LendingSignal | None:
        """Avalia um MarketRow e retorna LendingSignal se passar em todos os filtros."""

        # ── Filtro 1: Probabilidade ──────────────────────────────────────────
        yes_p = row.quote.yes_price
        no_p = row.quote.no_price

        if yes_p is None or no_p is None:
            rej["no_price"] += 1
            return None
        if not row.quote.has_liquidity:
            rej["no_liq"] += 1
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
            rej["low_prob"] += 1
            # Log top 3 mercados por probabilidade para debug
            best = max(yes_p, no_p)
            best_side = "YES" if yes_p >= no_p else "NO"
            if best >= 0.85:
                log.info(
                    "Prob BAIXA: %s %s @ %.1f%% — YES=%.3f NO=%.3f (min=%.2f) ETA=%dmin",
                    best_side, row.meta.question[:45], best * 100,
                    yes_p, no_p, config.BOT_MIN_PROBABILITY,
                    max(row.time_to_end_sec // 60, 0),
                )
            return None

        # ── Filtro 2: Tempo ──────────────────────────────────────────────────
        eta = row.time_to_end_sec
        if eta <= 0:
            rej["eta"] += 1
            return None  # ja passou
        if eta > config.BOT_MAX_MINUTES_TO_END * 60:
            rej["eta"] += 1
            return None  # muito longe

        # ── Filtro 3: Deduplicacao ───────────────────────────────────────────
        if row.meta.market_id in self._active_market_ids:
            rej["dedup"] += 1
            return None

        # ── Filtro 4: Book analysis (somente para candidatos validos) ────────
        book = self._book.analyze(token_id)
        if not book.is_tradeable:
            rej["book"] += 1
            log.info(
                "Book REJEITADO: %s %s @ %.1f%% — depth=$%.0f (min=$%.0f) spread=%.4f (max=%.4f)",
                side, row.meta.question[:40], probability * 100,
                book.depth_ask_usd, config.BOT_MIN_BOOK_DEPTH_USD,
                book.spread or 0, config.BOT_MAX_BOOK_SPREAD,
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
