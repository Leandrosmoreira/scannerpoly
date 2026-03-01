"""
bot_runner.py — Orquestrador do Lending Bot.

Modos:
  --dry-run (default): detecta sinais, loga, calcula P&L teorico. Sem ordens reais.
  --live             : executa ordens reais via py-clob-client. Requer credenciais .env.

Pipeline por ciclo:
  1. Discovery  — GammaClient lista mercados prestes a encerrar
  2. Pricing    — ClobClient busca precos/midpoints
  3. Filter     — SignalFilter avalia candidatos (prob, ETA, book)
  4. Risk Check — RiskManager verifica limites (exposure, losses, concurrent)
  5. Execute    — Executor coloca ordem limite GTC
  6. Track      — PositionManager registra posicao; PnLTracker loga sinais

Thread paralela (--live):
  resolution_loop — atualiza fills + verifica resolucao de mercados a cada 60s

Uso:
    python bot_runner.py [--window 60] [--interval 15] [--min-prob 0.97] [--once]
    python bot_runner.py --live [--max-position 50] [--max-exposure 250]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import config
from clob_client import ClobClient
from gamma_client import GammaClient
from models import MarketQuote, ScanResult
from scanner import _build_rows, _group_by_category
from bot.book_analyzer import BookAnalyzer
from bot.signal_filter import SignalFilter
from bot.pnl_tracker import PnLTracker
from bot.executor import Executor
from bot.position_manager import PositionManager
from bot.risk_manager import RiskManager

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lending_bot")

_running: bool = True


def _handle_shutdown(sig, frame):
    global _running
    log.info("Shutdown solicitado (sinal %s). Finalizando...", sig)
    _running = False


# ── Resolution loop (thread) ──────────────────────────────────────────────────

def _resolution_loop(
    position_manager: PositionManager,
    risk_manager: RiskManager,
    interval_sec: int = 60,
) -> None:
    """Thread que monitora fills e resolucoes de mercado a cada interval_sec."""
    log.info("Resolution loop iniciado (intervalo=%ds)", interval_sec)
    while _running:
        try:
            position_manager.update_fills()
            position_manager.check_resolutions()
            risk_manager.sync_resolved_positions()
        except Exception as exc:
            log.error("Resolution loop erro: %s", exc, exc_info=True)
        time.sleep(interval_sec)
    log.info("Resolution loop encerrado.")


# ── Bot loop ──────────────────────────────────────────────────────────────────

def run_bot(args: argparse.Namespace) -> None:
    global _running

    live_mode: bool = getattr(args, "live", False)

    gamma = GammaClient()
    clob = ClobClient()
    book_analyzer = BookAnalyzer()
    signal_filter = SignalFilter(book_analyzer)
    pnl = PnLTracker(live=live_mode)

    executor = Executor(dry_run=not live_mode)
    position_manager = PositionManager(executor)
    risk_manager = RiskManager(position_manager)

    window = args.window
    interval = args.interval
    mode_label = "LIVE" if live_mode else "DRY-RUN"

    log.info(
        "Lending Bot INICIADO (%s) | janela=%dmin | intervalo=%ds | prob_min=%.2f",
        mode_label, window, interval, args.min_prob,
    )

    if live_mode:
        # Inicia heartbeat (obrigatorio: CLOB cancela ordens se parar >10s)
        executor.start_heartbeat()

        # Inicia thread de resolucao
        res_thread = threading.Thread(
            target=_resolution_loop,
            args=(position_manager, risk_manager, 60),
            daemon=True,
            name="resolution-loop",
        )
        res_thread.start()

        balance = executor.get_balance_usdc()
        log.info("Saldo USDC disponivel: $%.2f", balance)

    prev: ScanResult | None = None
    cycle = 0

    try:
        while _running:
            cycle += 1
            t0 = time.monotonic()
            now = datetime.now(timezone.utc)
            end_window = now + timedelta(minutes=window)

            try:
                # ── 1. Discovery ─────────────────────────────────────────────
                markets_meta = gamma.list_markets_ending_soon(now, end_window)

                # ── 2. Pricing ───────────────────────────────────────────────
                quotes: dict[str, MarketQuote] = {}
                if markets_meta:
                    quotes = clob.fetch_quotes(markets_meta)

                # ── 3. Build rows ────────────────────────────────────────────
                rows, new_count, dropped_count = _build_rows(
                    markets_meta, quotes, now, prev
                )
                by_category = _group_by_category(rows)
                elapsed = time.monotonic() - t0

                result = ScanResult(
                    scan_ts=now,
                    cycle_num=cycle,
                    window_minutes=window,
                    markets=rows,
                    by_category=by_category,
                    elapsed_sec=elapsed,
                    new_count=new_count,
                    dropped_count=dropped_count,
                )

                # ── 4. Sincroniza IDs ja vistos / posicoes ativas ────────────
                active_ids = (
                    position_manager.get_active_market_ids()
                    if live_mode
                    else pnl._seen_market_ids
                )
                signal_filter.set_active_positions(active_ids)

                # ── 5. Filtrar sinais ────────────────────────────────────────
                signals = signal_filter.filter(result)

                # ── 6. Executar / logar ──────────────────────────────────────
                for s in signals:
                    if live_mode:
                        # Verificar risco
                        can_enter, reason = risk_manager.can_trade(s)
                        if not can_enter:
                            log.info(
                                "[RISK] Sinal BLOQUEADO (%s) — %s", s.question[:40], reason
                            )
                            pnl.log_signal(s)  # ainda loga para acompanhamento
                            continue

                        # Calcular tamanho
                        size_usd = risk_manager.size_position(s)

                        # Colocar ordem
                        order_id = executor.buy_limit(s, size_usd)
                        if order_id:
                            position_manager.open_position(s, order_id, size_usd)
                            pnl.log_signal(s)
                        else:
                            log.error(
                                "Ordem FALHOU para %s %s",
                                s.side, s.question[:40],
                            )
                    else:
                        # Dry-run: apenas loga
                        pnl.log_signal(s)

                # ── 7. Dashboard ─────────────────────────────────────────────
                pnl.print_summary(signals, cycle)

                # ── 8. Status de risco (live) ────────────────────────────────
                if live_mode:
                    realized = position_manager.get_realized_pnl()
                    log.info(
                        "[STATUS] %s | open=%d | realized=$%+.2f",
                        risk_manager.status_line(),
                        position_manager.get_open_count(),
                        realized,
                    )

                    # ── Log posicoes abertas com ETA ───────────────────────
                    open_positions = [
                        p for p in position_manager.get_all()
                        if p.order_status in ("pending", "filled") and not p.resolved
                    ]
                    if open_positions:
                        log.info("── POSIÇÕES ABERTAS (%d) ──", len(open_positions))
                        for p in open_positions:
                            eta_str = "?"
                            if p.expected_resolve_at:
                                eta_delta = p.expected_resolve_at - now
                                eta_min = max(int(eta_delta.total_seconds() // 60), 0)
                                if eta_min >= 60:
                                    eta_str = f"{eta_min // 60}h{eta_min % 60:02d}m"
                                else:
                                    eta_str = f"{eta_min}m"
                            log.info(
                                "  %s %s @ $%.2f x%.0f shares ($%.2f) | %s | ETA=%s | %s",
                                p.side, p.question[:40],
                                p.entry_price, p.size_shares, p.cost_usd,
                                p.order_status.upper(),
                                eta_str, p.order_id[:16],
                            )

                new_signals = sum(
                    1 for s in signals if s.market_id in pnl._seen_market_ids
                )
                log.info(
                    "[HEARTBEAT] ciclo #%d | %d mercados | %d sinais (%d novos) | %.1fs",
                    cycle, len(rows), len(signals), new_signals, elapsed,
                )

                prev = result

            except Exception as exc:
                log.error("Ciclo #%d falhou: %s", cycle, exc, exc_info=True)

            if args.once:
                break

            # Sleep compensado
            elapsed_total = time.monotonic() - t0
            sleep_for = max(0.0, interval - elapsed_total)
            if sleep_for > 0:
                time.sleep(sleep_for)

    finally:
        # Cleanup seguro
        if live_mode:
            log.info("Cancelando todas as ordens abertas...")
            executor.cancel_all()
            executor.stop()

        log.info(
            "Bot encerrado | %d sinais detectados | P&L teorico: $%+.2f",
            pnl.signals_logged, pnl.theoretical_pnl,
        )
        if live_mode:
            log.info(
                "P&L realizado: $%+.2f | %s",
                position_manager.get_realized_pnl(),
                risk_manager.status_line(),
            )


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Lending Bot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Executa ordens reais (requer credenciais .env). Default: dry-run.",
    )
    parser.add_argument(
        "--window", type=int, default=config.WINDOW_MINUTES,
        help="Janela de busca em minutos",
    )
    parser.add_argument(
        "--interval", type=int, default=config.BOT_SCAN_INTERVAL_SEC,
        help="Intervalo entre ciclos em segundos",
    )
    parser.add_argument(
        "--min-prob", type=float, default=config.BOT_MIN_PROBABILITY,
        help="Probabilidade minima para sinal",
    )
    parser.add_argument(
        "--min-depth", type=float, default=config.BOT_MIN_BOOK_DEPTH_USD,
        help="Profundidade minima do book (USD)",
    )
    parser.add_argument(
        "--max-position", type=float, default=config.BOT_MAX_POSITION_USD,
        help="Posicao maxima por trade (USD)",
    )
    parser.add_argument(
        "--max-exposure", type=float, default=config.BOT_MAX_TOTAL_EXPOSURE_USD,
        help="Exposure total maxima (USD)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Executa um ciclo e encerra",
    )
    parser.add_argument(
        "--tz", default=config.DISPLAY_TZ,
        help="Timezone para display",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # Aplicar overrides do CLI em config
    config.BOT_MIN_PROBABILITY = args.min_prob
    config.BOT_MIN_BOOK_DEPTH_USD = args.min_depth
    config.BOT_MAX_POSITION_USD = args.max_position
    config.BOT_MAX_TOTAL_EXPOSURE_USD = args.max_exposure
    config.DISPLAY_TZ = args.tz

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    run_bot(args)


if __name__ == "__main__":
    main()
