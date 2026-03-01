"""
executor.py — Execucao de ordens no Polymarket CLOB via py-clob-client.

Suporta dois modos:
- dry_run=True  : simula ordens sem enviar ao CLOB (safe)
- dry_run=False : ordens reais (requer credenciais em .env)

Credenciais carregadas de (em ordem):
  1. /root/scannerpoly/.env
  2. /root/bookpoly/.env
  3. Variaveis de ambiente do sistema

Variaveis necessarias:
  POLYMARKET_PRIVATE_KEY   — private key da wallet Polygon
  POLYMARKET_FUNDER        — endereco da wallet (proxy funder)
  POLYMARKET_API_KEY       — API key do CLOB (opcional, deriva se ausente)
  POLYMARKET_API_SECRET    — API secret
  POLYMARKET_PASSPHRASE    — passphrase
  POLYMARKET_SIGNATURE_TYPE — 1 = POLY_PROXY (Magic/Email wallet)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import config
from models import LendingSignal

log = logging.getLogger(__name__)

# Lazy import — py-clob-client pode nao estar instalado na maquina do dev
try:
    from py_clob_client.client import ClobClient as _ClobClientLib
    from py_clob_client.clob_types import (
        ApiCreds,
        AssetType,
        BalanceAllowanceParams,
        OrderArgs,
        PartialCreateOrderOptions,
    )
    from py_clob_client.order_builder.constants import BUY
    _CLOB_AVAILABLE = True
except ImportError:
    _CLOB_AVAILABLE = False
    log.warning("py-clob-client nao instalado. Modo live desativado.")

# Env files para tentar carregar credenciais
_ENV_PATHS = [
    Path("/root/scannerpoly/.env"),
    Path("/root/bookpoly/.env"),
    Path(os.path.dirname(os.path.dirname(__file__))) / ".env",
    Path(os.getcwd()) / ".env",
]


def _load_env() -> None:
    """Carrega .env do primeiro arquivo encontrado."""
    try:
        from dotenv import load_dotenv
        for p in _ENV_PATHS:
            if p.exists():
                load_dotenv(p, override=False)
                log.info("Credenciais carregadas de %s", p)
                return
        log.warning("Nenhum .env encontrado. Usando variaveis de ambiente.")
    except ImportError:
        log.warning("python-dotenv nao instalado. Usando variaveis de ambiente.")


class Executor:
    """
    Wrapper sobre py-clob-client para colocar ordens limite/mercado.
    Em dry_run, simula todas as operacoes sem tocar o CLOB.
    """

    CLOB_HOST = "https://clob.polymarket.com"

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self._client: object | None = None
        self._hb_thread: threading.Thread | None = None
        self._running = False
        self._order_counter = 0

        if not dry_run:
            self._init_client()

    # ── Inicializacao ────────────────────────────────────────────────────────

    def _init_client(self) -> None:
        if not _CLOB_AVAILABLE:
            raise RuntimeError(
                "py-clob-client nao instalado. "
                "Rode: pip install py-clob-client"
            )

        _load_env()

        pk = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        funder = os.getenv("POLYMARKET_FUNDER", "")
        api_key = os.getenv("POLYMARKET_API_KEY", "")
        api_secret = os.getenv("POLYMARKET_API_SECRET", "")
        passphrase = os.getenv("POLYMARKET_PASSPHRASE", "")
        sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))

        if not pk:
            raise ValueError(
                "POLYMARKET_PRIVATE_KEY nao encontrada. "
                "Configure .env em /root/scannerpoly/ ou /root/bookpoly/"
            )

        self._client = _ClobClientLib(
            self.CLOB_HOST,
            key=pk,
            chain_id=137,
            signature_type=sig_type,
            funder=funder,
        )

        if api_key and api_secret and passphrase:
            self._client.set_api_creds(ApiCreds(api_key, api_secret, passphrase))
            log.info("Executor: credenciais CLOB configuradas (API key existente)")
        else:
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            log.info("Executor: credenciais CLOB derivadas da private key")

    # ── Heartbeat ────────────────────────────────────────────────────────────

    def start_heartbeat(self) -> None:
        """
        Inicia thread de heartbeat. Lazy — so comeca quando ensure_heartbeat() for chamado.
        CLOB cancela ordens abertas se heartbeat parar > 10s.
        Sem ordens abertas, heartbeat nao eh necessario.
        """
        if self.dry_run:
            return
        self._running = True
        # Thread criada mas NAO iniciada — sera iniciada no primeiro ensure_heartbeat()
        self._hb_started = False
        log.info("Heartbeat configurado (lazy start — inicia ao colocar primeira ordem)")

    def ensure_heartbeat(self) -> None:
        """Garante que o heartbeat esta rodando. Chamar antes de colocar ordem."""
        if self.dry_run or self._hb_started:
            return
        self._hb_started = True
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="clob-heartbeat"
        )
        self._hb_thread.start()
        log.info("Heartbeat thread INICIADA (primeira ordem detectada)")

    def stop(self) -> None:
        self._running = False

    def _heartbeat_loop(self) -> None:
        # CLOB: primeiro POST com null retorna heartbeat_id no response.
        # Delay 3s no arranque para CLOB aceitar sessao.
        heartbeat_id: str | None = None
        fail_count = 0
        time.sleep(3)
        while self._running:
            try:
                resp = self._client.post_heartbeat(heartbeat_id)
                # Reusar heartbeat_id retornado (aceita ambos formatos da API)
                if isinstance(resp, dict):
                    heartbeat_id = resp.get("heartbeat_id") or resp.get("heartbeatId") or heartbeat_id
                if fail_count > 0:
                    log.info("Heartbeat recuperou apos %d falhas (id=%s)",
                             fail_count, heartbeat_id[:8] if heartbeat_id else "null")
                fail_count = 0
            except Exception as exc:
                fail_count += 1
                if fail_count <= 5:
                    log.warning("Heartbeat falhou (#%d): %s", fail_count, exc)
                elif fail_count == 6:
                    log.warning("Heartbeat falhou %dx — suprimindo logs seguintes", fail_count)
                # Em 400 continuo, proximo ciclo tenta de novo com null
                heartbeat_id = None
            time.sleep(5)

    # ── Balance ──────────────────────────────────────────────────────────────

    def get_balance_usdc(self) -> float:
        """Retorna saldo USDC disponivel na exchange."""
        if self.dry_run or self._client is None:
            return 9999.0

        try:
            result = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            # USDC tem 6 decimais — API retorna em micro-USDC
            raw = float(result.get("balance", 0))
            return raw / 1e6 if raw > 1_000_000 else raw
        except Exception as exc:
            log.warning("get_balance falhou: %s", exc)
            return 0.0

    # ── Ordens ───────────────────────────────────────────────────────────────

    def buy_limit(self, signal: LendingSignal, size_usd: float) -> str:
        """
        Coloca ordem limite GTC (maker, sem taxa).
        Retorna order_id.
        """
        size_shares = round(size_usd / signal.probability, 2)
        # Minimo de 5 shares por ordem
        if size_shares < 5.0:
            size_shares = 5.0
            size_usd = round(size_shares * signal.probability, 2)

        if self.dry_run:
            self._order_counter += 1
            oid = f"DRY_{signal.market_id[:8]}_{self._order_counter}"
            log.info(
                "[DRY] BUY LIMIT %s %.3f x %.3f = $%.2f | order_id=%s",
                signal.side, size_shares, signal.probability, size_usd, oid,
            )
            return oid

        try:
            # Garantir heartbeat rodando antes da primeira ordem
            self.ensure_heartbeat()

            neg_risk = getattr(signal, "neg_risk", False)
            resp = self._client.create_and_post_order(
                OrderArgs(
                    token_id=signal.token_id,
                    price=round(signal.probability, 2),
                    size=size_shares,
                    side=BUY,
                ),
                PartialCreateOrderOptions(
                    tick_size=config.BOT_TICK_SIZE,
                    neg_risk=neg_risk,
                ),
            )
            oid = resp.get("orderID", "")
            status = resp.get("status", "")
            log.info(
                "ORDEM COLOCADA: %s %.3f x%.2f = $%.2f | id=%s status=%s",
                signal.side, signal.probability, size_shares, size_usd, oid, status,
            )
            return oid
        except Exception as exc:
            log.error("buy_limit falhou: %s", exc)
            return ""

    def get_order(self, order_id: str) -> dict:
        """Consulta status de uma ordem."""
        if self.dry_run or order_id.startswith("DRY_"):
            # Simula fill imediato em dry-run
            return {"status": "matched", "size_matched": "100", "price": "0.99"}

        try:
            return self._client.get_order(order_id) or {}
        except Exception as exc:
            log.debug("get_order %s falhou: %s", order_id[:16], exc)
            return {}

    def cancel(self, order_id: str) -> bool:
        """Cancela uma ordem."""
        if self.dry_run or order_id.startswith("DRY_"):
            log.info("[DRY] CANCEL order_id=%s", order_id)
            return True

        try:
            self._client.cancel(order_id=order_id)
            return True
        except Exception as exc:
            log.warning("cancel %s falhou: %s", order_id[:16], exc)
            return False

    def get_open_orders(self) -> list[dict]:
        """Retorna lista de ordens abertas no CLOB."""
        if self.dry_run or self._client is None:
            return []
        try:
            resp = self._client.get_orders()
            if isinstance(resp, list):
                return resp
            return []
        except Exception as exc:
            log.warning("get_open_orders falhou: %s", exc)
            return []

    def get_trades(self) -> list[dict]:
        """Retorna historico de trades (fills) do CLOB."""
        if self.dry_run or self._client is None:
            return []
        try:
            resp = self._client.get_trades()
            if isinstance(resp, list):
                return resp
            return []
        except Exception as exc:
            log.warning("get_trades falhou: %s", exc)
            return []

    def cancel_all(self) -> None:
        """Cancela todas as ordens abertas (usado no shutdown)."""
        if self.dry_run:
            return
        try:
            self._client.cancel_all()
            log.info("Todas as ordens canceladas.")
        except Exception as exc:
            log.warning("cancel_all falhou: %s", exc)
