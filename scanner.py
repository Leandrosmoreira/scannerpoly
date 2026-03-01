"""
scanner.py — Orquestrador principal do Polymarket Close-Within-1h Scanner.

Uso:
    python scanner.py [--window 60] [--interval 60] [--output jsonl|sqlite|console|all]
                      [--changes-only] [--tz America/Sao_Paulo]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from clob_client import ClobClient
from formatters import Formatter
from gamma_client import GammaClient
from models import MarketMeta, MarketQuote, MarketRow, ScanResult
from storage import Storage

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scanner")

# ── Globals de ciclo ──────────────────────────────────────────────────────────

_storage: Optional[Storage] = None
_running: bool = True


def _handle_shutdown(sig, frame):
    global _running
    log.info("Shutdown solicitado (sinal %s). Finalizando...", sig)
    _running = False
    if _storage:
        _storage.flush()
    sys.exit(0)


# ── Lógica de ciclo ────────────────────────────────────────────────────────────

def _build_rows(
    markets: list[MarketMeta],
    quotes: dict[str, MarketQuote],
    now: datetime,
    prev: Optional[ScanResult],
) -> tuple[list[MarketRow], int, int]:
    """
    Constrói MarketRow, calcula deltas vs ciclo anterior e
    retorna (rows, new_count, dropped_count).
    """
    prev_ids = {r.meta.market_id for r in prev.markets} if prev else set()
    prev_prices: dict[str, tuple[float | None, float | None]] = {}
    if prev:
        for r in prev.markets:
            prev_prices[r.meta.market_id] = (r.quote.yes_price, r.quote.no_price)

    curr_ids = {m.market_id for m in markets}
    new_count = len(curr_ids - prev_ids)
    dropped_count = len(prev_ids - curr_ids)

    rows: list[MarketRow] = []
    for m in markets:
        quote = quotes.get(m.market_id, MarketQuote())
        eta = int((m.end_date - now).total_seconds())

        prev_yes, prev_no = prev_prices.get(m.market_id, (None, None))
        delta_yes = (
            round(quote.yes_price - prev_yes, 4)
            if quote.yes_price is not None and prev_yes is not None
            else None
        )
        delta_no = (
            round(quote.no_price - prev_no, 4)
            if quote.no_price is not None and prev_no is not None
            else None
        )

        rows.append(MarketRow(
            meta=m,
            quote=quote,
            time_to_end_sec=max(0, eta),
            is_new=(m.market_id not in prev_ids),
            price_delta_yes=delta_yes,
            price_delta_no=delta_no,
        ))

    # Ordenar por ETA crescente
    rows.sort(key=lambda r: r.time_to_end_sec)
    return rows, new_count, dropped_count


def _group_by_category(rows: list[MarketRow]) -> dict[str, list[MarketRow]]:
    groups: dict[str, list[MarketRow]] = {}
    for row in rows:
        cat = row.meta.category
        groups.setdefault(cat, []).append(row)
    return groups


def _should_print(result: ScanResult, prev: Optional[ScanResult]) -> bool:
    """Decide se imprime no console (respeita PRINT_ONLY_CHANGES)."""
    if not config.PRINT_ONLY_CHANGES:
        return True
    if prev is None:
        return True
    if result.new_count or result.dropped_count:
        return True
    # Verifica se algum preço mudou significativamente
    for row in result.markets:
        if row.price_delta_yes is not None and abs(row.price_delta_yes) >= config.PRICE_ALERT_THRESHOLD:
            return True
        if row.price_delta_no is not None and abs(row.price_delta_no) >= config.PRICE_ALERT_THRESHOLD:
            return True
    return False


def run_cycle(
    gamma: GammaClient,
    clob: ClobClient,
    storage: Storage,
    formatter: Formatter,
    prev: Optional[ScanResult],
    cycle_num: int,
    window_minutes: int,
) -> ScanResult:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    end_window = now + timedelta(minutes=window_minutes)

    log.info("Ciclo #%d iniciando | janela: %s → %s UTC",
             cycle_num,
             now.strftime("%H:%M:%S"),
             end_window.strftime("%H:%M:%S"))

    # 1. Discovery
    markets_meta = gamma.list_markets_ending_soon(now, end_window)

    # 2. Pricing (bulk + fallback)
    quotes: dict[str, MarketQuote] = {}
    if markets_meta:
        quotes = clob.fetch_quotes(markets_meta)

    # 3. Construir rows com deltas
    rows, new_count, dropped_count = _build_rows(markets_meta, quotes, now, prev)

    # 4. Agrupar por categoria
    by_category = _group_by_category(rows)

    elapsed = time.monotonic() - t0

    result = ScanResult(
        scan_ts=now,
        cycle_num=cycle_num,
        window_minutes=window_minutes,
        markets=rows,
        by_category=by_category,
        elapsed_sec=elapsed,
        new_count=new_count,
        dropped_count=dropped_count,
    )

    # 5. Output
    if _should_print(result, prev):
        formatter.print(result)
    else:
        log.info("Sem mudanças relevantes — impressão suprimida (PRINT_ONLY_CHANGES=true)")

    storage.write(result)

    log.info(
        "[HEARTBEAT] ciclo #%d | %d mercados | %d com preço | %.1fs",
        cycle_num,
        len(rows),
        sum(1 for r in rows if r.quote.has_liquidity),
        elapsed,
    )

    return result


# ── CLI e ponto de entrada ─────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Close-Within-1h Scanner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--window", type=int, default=config.WINDOW_MINUTES,
                        help="Janela de busca em minutos")
    parser.add_argument("--interval", type=int, default=config.SCAN_INTERVAL_SEC,
                        help="Intervalo entre ciclos em segundos")
    parser.add_argument("--output", default=config.OUTPUT_MODE,
                        choices=["console", "jsonl", "sqlite", "all"],
                        help="Modo de saída")
    parser.add_argument("--changes-only", action="store_true",
                        default=config.PRINT_ONLY_CHANGES,
                        help="Só imprime se houver mudanças relevantes")
    parser.add_argument("--tz", default=config.DISPLAY_TZ,
                        help="Timezone para exibição local (ex: America/Sao_Paulo)")
    parser.add_argument("--once", action="store_true",
                        help="Executa apenas um ciclo e encerra (útil para teste)")
    return parser.parse_args()


def main() -> None:
    global _storage, _running

    args = _parse_args()

    # Sobrescreve config com args do CLI
    config.WINDOW_MINUTES = args.window
    config.SCAN_INTERVAL_SEC = args.interval
    config.OUTPUT_MODE = args.output
    config.PRINT_ONLY_CHANGES = args.changes_only
    config.DISPLAY_TZ = args.tz

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    gamma = GammaClient()
    clob = ClobClient()
    _storage = Storage(mode=args.output)
    formatter = Formatter()

    log.info(
        "Scanner iniciado | janela=%dmin | intervalo=%ds | output=%s",
        args.window, args.interval, args.output,
    )

    prev: Optional[ScanResult] = None
    cycle = 1

    while _running:
        t_start = time.monotonic()
        try:
            prev = run_cycle(gamma, clob, _storage, formatter, prev, cycle, args.window)
        except Exception as exc:
            log.error("Ciclo #%d falhou inesperadamente: %s", cycle, exc, exc_info=True)

        cycle += 1

        if args.once:
            break

        elapsed = time.monotonic() - t_start
        sleep_for = max(0.0, args.interval - elapsed)
        if sleep_for > 0:
            log.debug("Aguardando %.1fs até próximo ciclo...", sleep_for)
            time.sleep(sleep_for)

    if _storage:
        _storage.flush()
    log.info("Scanner encerrado.")


if __name__ == "__main__":
    main()
