"""
risk_manager.py — Gerenciamento de risco do lending bot.

Controla:
- Limites de exposicao total e por posicao
- Numero maximo de posicoes simultaneas
- Loss limit horario com cooldown automatico
- Kelly-simplified sizing por probabilidade
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config
from models import LendingSignal
from bot.position_manager import PositionManager

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, position_manager: PositionManager) -> None:
        self._pm = position_manager
        self._hourly_loss: float = 0.0
        self._last_hour_reset: datetime = datetime.now(timezone.utc)
        self._cooldown_until: datetime | None = None
        self._wins: int = 0
        self._losses: int = 0

    # ── API publica ──────────────────────────────────────────────────────────

    def can_trade(self, signal: LendingSignal) -> tuple[bool, str]:
        """
        Verifica todos os limites de risco antes de entrar.
        Retorna (pode_entrar, motivo).
        """
        now = datetime.now(timezone.utc)

        # Reset loss horario
        if (now - self._last_hour_reset).total_seconds() > 3600:
            self._hourly_loss = 0.0
            self._last_hour_reset = now

        # Cooldown ativo
        if self._cooldown_until and now < self._cooldown_until:
            mins = int((self._cooldown_until - now).total_seconds() / 60)
            return False, f"Cooldown ativo ({mins}min restantes)"

        # Loss horario
        if self._hourly_loss >= config.BOT_MAX_HOURLY_LOSS_USD:
            self._cooldown_until = now + timedelta(seconds=config.BOT_LOSS_COOLDOWN_SEC)
            return False, (
                f"Loss horario ${self._hourly_loss:.2f} atingiu "
                f"limite ${config.BOT_MAX_HOURLY_LOSS_USD:.2f}"
            )

        # Exposure total
        exposure = self._pm.get_total_exposure()
        size = self.size_position(signal)
        if exposure + size > config.BOT_MAX_TOTAL_EXPOSURE_USD:
            return False, (
                f"Exposure ${exposure:.2f}+${size:.2f} excederia "
                f"limite ${config.BOT_MAX_TOTAL_EXPOSURE_USD:.2f}"
            )

        # Posicoes simultaneas
        if self._pm.get_open_count() >= config.BOT_MAX_CONCURRENT_POSITIONS:
            return False, (
                f"Maximo de {config.BOT_MAX_CONCURRENT_POSITIONS} "
                "posicoes simultaneas atingido"
            )

        return True, "OK"

    def size_position(self, signal: LendingSignal) -> float:
        """
        Kelly-simplified: escala o tamanho pela probabilidade.

        prob >= 0.999 → 100% do max
        prob >= 0.99  → 80% do max
        prob >= 0.98  → 50% do max
        prob >= 0.97  → 30% do max (minimo aceito)
        """
        p = signal.probability
        max_usd = config.BOT_MAX_POSITION_USD

        if p >= 0.999:
            factor = 1.0
        elif p >= 0.99:
            factor = 0.8
        elif p >= 0.98:
            factor = 0.5
        elif p >= 0.96:
            factor = 0.3
        elif p >= 0.94:
            factor = 0.2
        else:
            factor = 0.15  # 93-94%: conservador

        size = max_usd * factor

        # Nunca mais que 1/10 do capital total
        max_safe = config.BOT_MAX_TOTAL_EXPOSURE_USD / 10
        size = min(size, max_safe)

        # Minimo Polymarket: $5
        size = max(size, 5.0)

        return round(size, 2)

    def register_win(self, pnl: float) -> None:
        self._wins += 1
        log.info(
            "WIN registrado | pnl=$%.2f | total wins=%d losses=%d",
            pnl, self._wins, self._losses,
        )

    def register_loss(self, loss_usd: float) -> None:
        self._losses += 1
        self._hourly_loss += abs(loss_usd)
        log.warning(
            "LOSS registrado | $%.2f | horario=$%.2f/%.2f | wins=%d losses=%d",
            loss_usd, self._hourly_loss, config.BOT_MAX_HOURLY_LOSS_USD,
            self._wins, self._losses,
        )

        if self._hourly_loss >= config.BOT_MAX_HOURLY_LOSS_USD:
            now = datetime.now(timezone.utc)
            self._cooldown_until = now + timedelta(seconds=config.BOT_LOSS_COOLDOWN_SEC)
            log.warning(
                "LOSS LIMIT ATINGIDO — cooldown ate %s",
                self._cooldown_until.strftime("%H:%M:%S"),
            )

    def sync_resolved_positions(self) -> None:
        """
        Atualiza contadores de wins/losses com base em posicoes recém-resolvidas.
        Deve ser chamado apos position_manager.check_resolutions().
        """
        for pos in self._pm.get_resolved():
            if pos.pnl is None:
                continue
            if pos.won and pos.pnl > 0:
                self.register_win(pos.pnl)
                # Zera o pnl para nao contar novamente
                pos.pnl = None
            elif pos.won is False and pos.pnl is not None and pos.pnl < 0:
                self.register_loss(abs(pos.pnl))
                pos.pnl = None

    # ── Estatísticas ─────────────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        total = self._wins + self._losses
        return self._wins / total if total > 0 else 0.0

    @property
    def total_trades(self) -> int:
        return self._wins + self._losses

    def status_line(self) -> str:
        return (
            f"W={self._wins} L={self._losses} "
            f"rate={self.win_rate:.0%} "
            f"exposure=${self._pm.get_total_exposure():.0f} "
            f"hourly_loss=${self._hourly_loss:.2f}"
        )
