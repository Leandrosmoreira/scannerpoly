"""
bot_runner.py — Orquestrador do Lending Bot (Fase 1: Dry-Run / Signal Detection).

Roda em cima do scanner existente:
1. Usa GammaClient para descobrir mercados
2. Usa ClobClient para pricing
3. SignalFilter avalia quais mercados sao candidatos (prob >= 97%, liquidez, etc.)
4. BookAnalyzer verifica profundidade do order book em tempo real
5. PnLTracker loga sinais e simula P&L

Modo dry-run: NAO executa ordens. Apenas detecta e loga sinais.

Uso:
    python bot_runner.py [--window 60] [--interval 15] [--min-prob 0.97] [--once]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import config
from clob_client import ClobClient
from gamma_client import GammaClient
from models import MarketQuote, MarketRow, ScanResult
from scanner import _build_rows, _group_by_category
from bot.book_analyzer import BookAnalyzer
from bot.signal_filter import SignalFilter
from bot.pnl_tracker import PnLTracker

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
    sys.exit(0)


# ── Bot loop ──────────────────────────────────────────────────────────────────

def run_bot(args: argparse.Namespace) -> None:
    global _running

    gamma = GammaClient()
    clob = ClobClient()
    book_analyzer = BookAnalyzer()
    signal_filter = SignalFilter(book_analyzer)
    pnl = PnLTracker()

    window = args.window
    interval = args.interval

    log.info(
        "Lending Bot INICIADO (DRY-RUN) | janela=%dmin | intervalo=%ds | prob_min=%.2f",
        window, interval, args.min_prob,
    )

    prev: ScanResult | None = None
    cycle = 0

    while _running:
        cycle += 1
        t0 = time.monotonic()
        now = datetime.now(timezone.utc)
        end_window = now + timedelta(minutes=window)

        try:
            # ── 1. Discovery ─────────────────────────────────────────────────
            markets_meta = gamma.list_markets_ending_soon(now, end_window)

            # ── 2. Pricing ───────────────────────────────────────────────────
            quotes: dict[str, MarketQuote] = {}
            if markets_meta:
                quotes = clob.fetch_quotes(markets_meta)

            # ── 3. Build rows (reutiliza logica do scanner) ──────────────────
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

            # ── 4. Filtrar sinais ────────────────────────────────────────────
            signals = signal_filter.filter(result)

            # ── 5. Logar cada sinal (dry-run) ────────────────────────────────
            for s in signals:
                pnl.log_signal(s)

            # ── 6. Dashboard ─────────────────────────────────────────────────
            pnl.print_summary(signals, cycle)

            log.info(
                "[HEARTBEAT] ciclo #%d | %d mercados | %d sinais | %.1fs",
                cycle, len(rows), len(signals), elapsed,
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

    log.info(
        "Bot encerrado | %d sinais detectados | P&L teorico: $%+.2f",
        pnl.signals_logged, pnl.theoretical_pnl,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Lending Bot — Phase 1: Dry-Run Signal Detection",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--window", type=int, default=config.WINDOW_MINUTES,
                        help="Janela de busca em minutos")
    parser.add_argument("--interval", type=int, default=config.BOT_SCAN_INTERVAL_SEC,
                        help="Intervalo entre ciclos em segundos")
    parser.add_argument("--min-prob", type=float, default=config.BOT_MIN_PROBABILITY,
                        help="Probabilidade minima para sinal")
    parser.add_argument("--min-depth", type=float, default=config.BOT_MIN_BOOK_DEPTH_USD,
                        help="Profundidade minima do book (USD)")
    parser.add_argument("--max-position", type=float, default=config.BOT_MAX_POSITION_USD,
                        help="Posicao maxima por trade (USD)")
    parser.add_argument("--once", action="store_true",
                        help="Executa um ciclo e encerra")
    parser.add_argument("--tz", default=config.DISPLAY_TZ,
                        help="Timezone para display")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # Aplicar overrides do CLI
    config.BOT_MIN_PROBABILITY = args.min_prob
    config.BOT_MIN_BOOK_DEPTH_USD = args.min_depth
    config.BOT_MAX_POSITION_USD = args.max_position
    config.DISPLAY_TZ = args.tz

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    run_bot(args)


if __name__ == "__main__":
    main()
