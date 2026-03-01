"""
formatters.py — Impressão de snapshots no terminal com rich.
Fallback para impressão simples se rich não estiver disponível.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from models import MarketRow, ScanResult

log = logging.getLogger(__name__)

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.text import Text
    _RICH = True
    _console = Console()
except ImportError:
    _RICH = False
    _console = None


def _local_tz():
    try:
        return ZoneInfo(config.DISPLAY_TZ)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _fmt_eta(seconds: int) -> str:
    if seconds <= 0:
        return "  0s   "
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:2d}h{m:02d}m"
    return f"{m:2d}m{s:02d}s"


def _fmt_price(price: float | None) -> str:
    if price is None:
        return "  —   "
    return f"{price:.3f}"


def _fmt_delta(delta: float | None) -> str:
    if delta is None or abs(delta) < 0.001:
        return ""
    sign = "▲" if delta > 0 else "▼"
    return f"{sign}{abs(delta):.3f}"


def _eta_style(seconds: int) -> str:
    """Cor para rich baseada no ETA."""
    if seconds <= 300:    # ≤ 5 min
        return "bold red"
    if seconds <= 900:    # ≤ 15 min
        return "yellow"
    return "green"


def _truncate(text: str, max_len: int = 55) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


# ── Formatador principal ────────────────────────────────────────────────────────

class Formatter:
    def __init__(self) -> None:
        self._tz = _local_tz()

    def print(self, result: ScanResult) -> None:
        if _RICH:
            self._print_rich(result)
        else:
            self._print_plain(result)

    # ── Rich ───────────────────────────────────────────────────────────────────

    def _print_rich(self, result: ScanResult) -> None:
        now_utc = result.scan_ts
        now_local = now_utc.astimezone(self._tz)
        tz_label = config.DISPLAY_TZ.split("/")[-1]

        # ── Cabeçalho ──
        _console.print()
        _console.rule(
            f"[bold cyan]SCAN #{result.cycle_num}[/]  "
            f"{now_utc:%Y-%m-%d %H:%M:%S} UTC | "
            f"{now_local:%H:%M:%S} {tz_label}"
        )

        total = len(result.markets)
        liq = sum(1 for r in result.markets if r.quote.has_liquidity)
        _console.print(
            f"  [bold]{total}[/] mercados encerrando na próxima "
            f"[bold]{result.window_minutes}min[/]  "
            f"[dim]({liq} com preço | ciclo em {result.elapsed_sec:.1f}s)[/]"
        )

        if result.new_count or result.dropped_count:
            _console.print(
                f"  [green]★ {result.new_count} novos[/]  "
                f"[red]✕ {result.dropped_count} saíram[/]"
            )

        # Top categorias
        top = sorted(result.by_category.items(), key=lambda x: -len(x[1]))
        top_str = "  ".join(
            f"[cyan]{cat}[/]([bold]{len(rows)}[/])"
            for cat, rows in top[: config.TOP_CATEGORIES_DISPLAY]
        )
        _console.print(f"  {top_str}")
        _console.print()

        # ── Tabela ──
        table = Table(
            box=box.SIMPLE_HEAD,
            show_edge=False,
            pad_edge=False,
            expand=False,
        )
        table.add_column("ETA", style="", no_wrap=True, width=7)
        table.add_column("YES", justify="right", width=6)
        table.add_column("NO", justify="right", width=6)
        table.add_column("SPR", justify="right", width=6)
        table.add_column("Δ", width=6)
        table.add_column("SRC", width=5)
        table.add_column("CAT", width=12)
        table.add_column("TÍTULO", min_width=30)

        rows = result.markets[: config.TOP_MARKETS_DISPLAY]
        for row in rows:
            eta_s = row.time_to_end_sec
            eta_txt = Text(_fmt_eta(eta_s), style=_eta_style(eta_s))
            new_pfx = "★ " if row.is_new else "  "

            delta_str = _fmt_delta(row.price_delta_yes)
            alert_style = "bold yellow" if abs(row.price_delta_yes or 0) >= config.PRICE_ALERT_THRESHOLD else ""

            spr_val = row.quote.spread
            spr_str = f"{spr_val:+.3f}" if spr_val is not None else "  —  "
            spr_style = "red" if spr_val is not None and abs(spr_val) > 0.05 else ""

            table.add_row(
                eta_txt,
                _fmt_price(row.quote.yes_price),
                _fmt_price(row.quote.no_price),
                Text(spr_str, style=spr_style),
                Text(delta_str, style=alert_style),
                row.quote.price_source[:5],
                _truncate(row.meta.category, 12),
                new_pfx + _truncate(row.meta.question, 53),
            )

        _console.print(table)

        hidden = total - len(rows)
        no_liq = total - liq
        if hidden > 0:
            _console.print(f"  [dim]+ {hidden} outros mercados não exibidos[/]")
        if no_liq > 0:
            _console.print(f"  [dim]{no_liq} mercado(s) sem liquidez (preço indisponível)[/]")
        _console.print()

    # ── Plain (fallback sem rich) ──────────────────────────────────────────────

    def _print_plain(self, result: ScanResult) -> None:
        now_utc = result.scan_ts
        now_local = now_utc.astimezone(self._tz)
        tz_label = config.DISPLAY_TZ.split("/")[-1]
        sep = "=" * 80

        print(f"\n{sep}")
        print(
            f"SCAN #{result.cycle_num}  "
            f"{now_utc:%Y-%m-%d %H:%M:%S} UTC | {now_local:%H:%M:%S} {tz_label}"
        )
        total = len(result.markets)
        liq = sum(1 for r in result.markets if r.quote.has_liquidity)
        print(
            f"{total} mercados encerrando em {result.window_minutes}min  "
            f"({liq} com preço | ciclo {result.elapsed_sec:.1f}s)"
        )
        if result.new_count or result.dropped_count:
            print(f"Novos: {result.new_count}  |  Saíram: {result.dropped_count}")

        top = sorted(result.by_category.items(), key=lambda x: -len(x[1]))
        top_str = "  ".join(
            f"{cat}({len(rows)})" for cat, rows in top[: config.TOP_CATEGORIES_DISPLAY]
        )
        print(f"Top: {top_str}")
        print("-" * 80)
        print(f"{'ETA':>7}  {'YES':>6}  {'NO':>6}  {'SPR':>6}  {'SRC':<5}  {'CAT':<12}  TÍTULO")
        print("-" * 80)

        for row in result.markets[: config.TOP_MARKETS_DISPLAY]:
            new_pfx = "* " if row.is_new else "  "
            delta = _fmt_delta(row.price_delta_yes)
            print(
                f"{_fmt_eta(row.time_to_end_sec):>7}  "
                f"{_fmt_price(row.quote.yes_price):>6}  "
                f"{_fmt_price(row.quote.no_price):>6}  "
                f"{(f'{row.quote.spread:+.3f}' if row.quote.spread is not None else '  —  '):>6}  "
                f"{row.quote.price_source[:5]:<5}  "
                f"{_truncate(row.meta.category, 12):<12}  "
                f"{new_pfx}{_truncate(row.meta.question, 50)}"
                + (f"  {delta}" if delta else "")
            )

        hidden = total - len(result.markets[: config.TOP_MARKETS_DISPLAY])
        if hidden > 0:
            print(f"  + {hidden} outros não exibidos")
        print(sep)
