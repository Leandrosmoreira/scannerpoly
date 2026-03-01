"""
position_manager.py — Gerenciamento de posicoes abertas pelo bot.

Responsabilidades:
- Registrar novas posicoes (apos ordem colocada)
- Atualizar status de fills (poll CLOB)
- Verificar resolucao de mercados (poll Gamma API)
- Calcular P&L realizado
- Persistir estado em JSONL (sobrevive a restarts)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import requests

import config
from models import BotPosition, LendingSignal
from bot.executor import Executor

log = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, executor: Executor) -> None:
        self._executor = executor
        self._positions: dict[str, BotPosition] = {}  # position_id -> BotPosition
        self._market_to_pos: dict[str, str] = {}       # market_id -> position_id
        self._pos_log = os.path.join(config.DATA_DIR, "positions.jsonl")
        self._gamma_session = requests.Session()
        self._gamma_session.headers.update({"Accept": "application/json"})

    # ── API publica ──────────────────────────────────────────────────────────

    def open_position(
        self, signal: LendingSignal, order_id: str, size_usd: float
    ) -> BotPosition:
        """Registra nova posicao apos ordem colocada."""
        now = datetime.now(timezone.utc)
        pos = BotPosition(
            position_id=str(uuid4()),
            market_id=signal.market_id,
            condition_id=signal.condition_id,
            question=signal.question,
            token_id=signal.token_id,
            side=signal.side,
            entry_price=signal.probability,
            size_shares=round(size_usd / signal.probability, 2),
            cost_usd=round(size_usd, 2),
            order_id=order_id,
            order_status="pending",
            entered_at=now,
            # Espera resolucao: ETA + 3h (tempo de oracle UMA)
            expected_resolve_at=now + timedelta(seconds=signal.time_to_end_sec + 3 * 3600),
        )
        self._positions[pos.position_id] = pos
        self._market_to_pos[pos.market_id] = pos.position_id
        self._persist(pos)
        log.info(
            "Posicao aberta: %s %s %.3f x%.2f = $%.2f | order=%s",
            pos.side, pos.question[:35], pos.entry_price,
            pos.size_shares, pos.cost_usd, order_id[:16],
        )
        return pos

    def update_fills(self) -> None:
        """Poll CLOB para atualizar status de fills das ordens pendentes."""
        for pos in self._positions.values():
            if pos.order_status in ("filled", "cancelled", "failed"):
                continue
            if pos.order_id.startswith("DRY_"):
                # Em dry-run, simula fill imediato
                pos.order_status = "filled"
                pos.fill_price = pos.entry_price
                pos.fill_size = pos.size_shares
                continue

            order_data = self._executor.get_order(pos.order_id)
            status = order_data.get("status", "")

            if status in ("matched", "confirmed", "CONFIRMED", "MINED"):
                old_status = pos.order_status
                pos.order_status = "filled"
                size_matched = order_data.get("size_matched") or order_data.get("original_size")
                fill_price = order_data.get("price") or order_data.get("maker_amount")
                if size_matched:
                    try:
                        pos.fill_size = float(size_matched)
                    except (TypeError, ValueError):
                        pos.fill_size = pos.size_shares
                if fill_price:
                    try:
                        pos.fill_price = float(fill_price)
                    except (TypeError, ValueError):
                        pos.fill_price = pos.entry_price
                # Atualiza cost com fill real
                if pos.fill_price and pos.fill_size:
                    pos.cost_usd = round(pos.fill_price * pos.fill_size, 2)
                if old_status != "filled":
                    log.info(
                        "Fill confirmado: %s %s | %.2f shares @ %.3f",
                        pos.side, pos.question[:30],
                        pos.fill_size or 0, pos.fill_price or 0,
                    )

            elif status in ("cancelled", "FAILED"):
                pos.order_status = "cancelled" if "cancel" in status.lower() else "failed"
                log.warning("Ordem %s status: %s", pos.order_id[:16], status)

    def check_resolutions(self) -> None:
        """
        Poll Gamma API para verificar se mercados resolveram.
        Chama apenas posicoes com fill confirmado e nao resolvidas.
        """
        for pos in self._positions.values():
            if pos.resolved:
                continue
            if pos.order_status not in ("filled",):
                continue

            market_data = self._fetch_market(pos.market_id)
            if not market_data:
                continue

            resolved = market_data.get("resolved") or market_data.get("closed", False)
            if not resolved:
                continue

            # Determina resultado
            winner = self._extract_winner(market_data)
            if winner is None:
                log.debug("Mercado %s resolvido mas resultado nao identificado", pos.market_id)
                continue

            pos.resolved = True
            pos.won = (winner.upper() == pos.side.upper())
            fill_size = pos.fill_size or pos.size_shares

            if pos.won:
                pos.payout = round(fill_size * 1.0, 4)
                pos.pnl = round(pos.payout - pos.cost_usd, 4)
                log.info(
                    "WIN: %s %s | payout=$%.2f | pnl=$%.2f",
                    pos.side, pos.question[:35], pos.payout, pos.pnl,
                )
            else:
                pos.payout = 0.0
                pos.pnl = round(-pos.cost_usd, 4)
                log.warning(
                    "LOSS: %s %s | perdeu=$%.2f",
                    pos.side, pos.question[:35], pos.cost_usd,
                )

            self._persist(pos)

    def get_active_market_ids(self) -> set[str]:
        """IDs de mercados com posicao ativa (nao resolvida, nao cancelada)."""
        return {
            p.market_id
            for p in self._positions.values()
            if p.order_status in ("pending", "filled") and not p.resolved
        }

    def get_total_exposure(self) -> float:
        """Total em USD em posicoes abertas nao resolvidas."""
        return sum(
            p.cost_usd
            for p in self._positions.values()
            if p.order_status in ("pending", "filled") and not p.resolved
        )

    def get_open_count(self) -> int:
        return sum(
            1 for p in self._positions.values()
            if p.order_status in ("pending", "filled") and not p.resolved
        )

    def get_realized_pnl(self) -> float:
        return sum(p.pnl or 0 for p in self._positions.values() if p.resolved)

    def get_all(self) -> list[BotPosition]:
        return list(self._positions.values())

    def get_resolved(self) -> list[BotPosition]:
        return [p for p in self._positions.values() if p.resolved]

    # ── Internos ─────────────────────────────────────────────────────────────

    def _fetch_market(self, market_id: str) -> dict | None:
        """Busca dados do mercado na Gamma API pelo ID."""
        try:
            resp = self._gamma_session.get(
                config.GAMMA_BASE + "/markets",
                params={"id": market_id},
                timeout=config.REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict):
                return data
        except Exception as exc:
            log.debug("_fetch_market %s falhou: %s", market_id, exc)
        return None

    @staticmethod
    def _extract_winner(market_data: dict) -> str | None:
        """
        Extrai o lado vencedor (YES/NO) dos dados do mercado.
        Tenta varios campos conhecidos da Gamma API.
        """
        # Campo direto
        for field in ("resolutionResult", "resolution_result", "resolution", "outcome"):
            val = market_data.get(field)
            if val and str(val).strip().upper() in ("YES", "NO"):
                return str(val).strip().upper()

        # Via outcomes: o winning_outcome pode ser o indice ou o label
        outcomes_raw = market_data.get("outcomes")
        winning = market_data.get("winningOutcome") or market_data.get("winning_outcome")
        if outcomes_raw and winning is not None:
            try:
                import json as _json
                outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                if isinstance(outcomes, list) and isinstance(winning, int):
                    label = outcomes[winning]
                    if str(label).upper() in ("YES", "NO"):
                        return str(label).upper()
                elif isinstance(winning, str) and winning.upper() in ("YES", "NO"):
                    return winning.upper()
            except Exception:
                pass

        return None

    def _persist(self, pos: BotPosition) -> None:
        """Salva/atualiza posicao em JSONL."""
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            row = {
                "position_id": pos.position_id,
                "market_id": pos.market_id,
                "question": pos.question,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "size_shares": pos.size_shares,
                "cost_usd": pos.cost_usd,
                "order_id": pos.order_id,
                "order_status": pos.order_status,
                "fill_price": pos.fill_price,
                "fill_size": pos.fill_size,
                "entered_at": pos.entered_at.isoformat(),
                "resolved": pos.resolved,
                "won": pos.won,
                "payout": pos.payout,
                "pnl": pos.pnl,
                "ts_write": datetime.now(timezone.utc).isoformat(),
            }
            with open(self._pos_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Falha ao persistir posicao: %s", exc)
