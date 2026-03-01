"""
formatters.py — Impressão de snapshots no terminal com rich.
Novo layout amigável com categoria visível e melhor hierarquia visual.
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

# Mapa de emojis por categoria
CATEGORY_EMOJI = {
    "Sports": "⚽",
    "Politics": "🏛️",
    "Crypto": "🔗",
    "Finance": "💰",
    "Weather": "🌦️",
    "Election": "🗳️",
    "Soccer": "⚽",
    "Tennis": "🎾",
    "Basketball": "🏀",
    "Baseball": "⚾",
    "Football": "🏈",
    "Hockey": "🏒",
    "Golf": "⛳",
    "MMA": "🥋",
    "Boxing": "🥊",
    "Other": "📊",
}


def _local_tz():
    try:
        return ZoneInfo(config.DISPLAY_TZ)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _fmt_eta(seconds: int) -> str:
    """Formata ETA em minutos:segundos ou horas:minutos."""
    if seconds <= 0:
        return "  0s"
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:2d}h{m:02d}m"
    return f"{m:2d}m{s:02d}s"


def _fmt_prices(yes: float | None, no: float | None) -> str:
    """Formata YES/NO em linha única e compacta."""
    if yes is None or no is None:
        return "  —  /  — "
    return f"{yes:.2f}/{no:.2f}"


def _fmt_spread(spread: float | None) -> str:
    """Formata spread com direção."""
    if spread is None or abs(spread) < 0.001:
        return ""
    sign = "▲" if spread > 0 else "▼"
    return f"{sign}{abs(spread):.3f}"


def _fmt_delta(delta: float | None) -> str:
    """Formata delta de preço entre ciclos."""
    if delta is None or abs(delta) < 0.001:
        return ""
    sign = "↑" if delta > 0 else "↓"
    return f"{sign}{abs(delta):.3f}"


def _eta_style(seconds: int) -> str:
    """Cor para rich baseada no ETA."""
    if seconds <= 180:    # ≤ 3 min
        return "bold red"
    if seconds <= 300:    # ≤ 5 min
        return "red"
    if seconds <= 900:    # ≤ 15 min
        return "yellow"
    return "green"


def _category_with_emoji(cat: str) -> str:
    """Retorna categoria com emoji apropriado."""
    emoji = CATEGORY_EMOJI.get(cat, CATEGORY_EMOJI["Other"])
    return f"{emoji} {cat[:15]}"


def _truncate(text: str, max_len: int = 60) -> str:
    """Trunca texto com elipsis."""
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
        total = len(result.markets)
        liq = sum(1 for r in result.markets if r.quote.has_liquidity)

        # ── Cabeçalho ─────────────────────────────────────────────────────────
        _console.print()
        _console.rule(
            f"[bold cyan]SCAN #{result.cycle_num}[/]  "
            f"[white]{now_utc:%Y-%m-%d %H:%M:%S} UTC[/]  "
            f"[dim]·[/]  [white]{now_local:%H:%M:%S} {tz_label}[/]",
            style="dim cyan",
        )

        # Linha de stats
        stats_parts = [
            f"[bold white]{total}[/] [dim]mercados[/]",
            f"[green]{liq} com preço[/]",
            f"[dim]ciclo: {result.elapsed_sec:.1f}s[/]",
        ]
        if result.new_count:
            stats_parts.append(f"[bold green]★ {result.new_count} novos[/]")
        if result.dropped_count:
            stats_parts.append(f"[bold red]✕ {result.dropped_count} saíram[/]")
        _console.print("  " + "  [dim]·[/]  ".join(stats_parts))

        # Badges de categoria
        top_cats = sorted(result.by_category.items(), key=lambda x: -len(x[1]))
        badges: list[str] = []
        bg_colors = ["blue", "dark_red", "dark_green", "magenta", "dark_orange"]
        for i, (cat, rows) in enumerate(top_cats[: config.TOP_CATEGORIES_DISPLAY]):
            emoji = CATEGORY_EMOJI.get(cat, "📊")
            color = bg_colors[i % len(bg_colors)]
            badges.append(f"[bold white on {color}] {emoji} {cat[:12]} {len(rows)} [/]")
        _console.print("  " + "  ".join(badges))
        _console.print()

        # ── Tabela ────────────────────────────────────────────────────────────
        table = Table(
            box=box.SIMPLE_HEAD,
            show_edge=False,
            pad_edge=True,
            expand=False,
            show_header=True,
            header_style="bold dim",
        )
        table.add_column("ETA",       no_wrap=True, width=7,  justify="right")
        table.add_column("YES / NO",  no_wrap=True, width=11, justify="center")
        table.add_column("Δ",         no_wrap=True, width=7,  justify="center")
        table.add_column("CATEGORIA", no_wrap=True, width=22)
        table.add_column("MERCADO",   min_width=36)

        for row in result.markets[: config.TOP_MARKETS_DISPLAY]:
            eta_s = row.time_to_end_sec

            # ETA colorido por urgência
            eta_txt = Text(_fmt_eta(eta_s), style=_eta_style(eta_s), justify="right")

            # YES/NO compacto — vermelho se sem liquidez
            prices_str = _fmt_prices(row.quote.yes_price, row.quote.no_price)
            if not row.quote.has_liquidity:
                prices_txt = Text(prices_str, style="dim")
            else:
                prices_txt = Text(prices_str, style="bold white")

            # Delta — destaque amarelo se alerta
            delta_str = _fmt_delta(row.price_delta_yes)
            is_alert = abs(row.price_delta_yes or 0) >= config.PRICE_ALERT_THRESHOLD
            delta_txt = Text(delta_str, style="bold yellow" if is_alert else "dim")

            # Spread inline no preço — adiciona sinal vermelho se largo
            spr = _fmt_spread(row.quote.spread)
            if spr and row.quote.spread is not None and abs(row.quote.spread) > 0.05:
                prices_txt.append(f" {spr}", style="red")

            # Categoria com emoji + badge de cor por tipo
            cat = row.meta.category
            cat_txt = Text(_category_with_emoji(cat), style="cyan")

            # Título com prefixo ★ para novo e ⚡ para alerta de preço
            if row.is_new:
                prefix = "[bold green]★[/] "
            elif is_alert:
                prefix = "[bold yellow]⚡[/] "
            else:
                prefix = "  "
            title_txt = Text.from_markup(prefix + _truncate(row.meta.question, 56))

            table.add_row(eta_txt, prices_txt, delta_txt, cat_txt, title_txt)

        _console.print(table)

        # Rodapé
        hidden = total - min(total, config.TOP_MARKETS_DISPLAY)
        no_liq = total - liq
        footer_parts: list[str] = []
        if hidden > 0:
            footer_parts.append(f"[dim]+ {hidden} não exibidos[/]")
        if no_liq > 0:
            footer_parts.append(f"[dim]{no_liq} sem liquidez[/]")
        if footer_parts:
            _console.print("  " + "  ·  ".join(footer_parts))
        _console.print()

    # ── Plain (fallback sem rich) ──────────────────────────────────────────────

    def _print_plain(self, result: ScanResult) -> None:
        now_utc = result.scan_ts
        now_local = now_utc.astimezone(self._tz)
        tz_label = config.DISPLAY_TZ.split("/")[-1]
        W = 90
        sep = "=" * W

        print(f"\n{sep}")
        print(
            f"  SCAN #{result.cycle_num}  ·  "
            f"{now_utc:%Y-%m-%d %H:%M:%S} UTC  ·  {now_local:%H:%M:%S} {tz_label}"
        )

        total = len(result.markets)
        liq = sum(1 for r in result.markets if r.quote.has_liquidity)
        stats = f"  {total} mercados  ·  {liq} com preço  ·  ciclo: {result.elapsed_sec:.1f}s"
        if result.new_count:
            stats += f"  ·  ★ {result.new_count} novos"
        if result.dropped_count:
            stats += f"  ·  ✕ {result.dropped_count} saíram"
        print(stats)

        # Categorias
        top = sorted(result.by_category.items(), key=lambda x: -len(x[1]))
        cat_str = "  ".join(
            f"{CATEGORY_EMOJI.get(cat, '📊')} {cat}({len(rows)})"
            for cat, rows in top[: config.TOP_CATEGORIES_DISPLAY]
        )
        print(f"  {cat_str}")
        print("-" * W)
        print(f"  {'ETA':>7}  {'YES/NO':<11}  {'Δ':<7}  {'CATEGORIA':<20}  MERCADO")
        print("-" * W)

        for row in result.markets[: config.TOP_MARKETS_DISPLAY]:
            delta_str = _fmt_delta(row.price_delta_yes)
            is_alert = abs(row.price_delta_yes or 0) >= config.PRICE_ALERT_THRESHOLD
            spr = _fmt_spread(row.quote.spread)
            prices = _fmt_prices(row.quote.yes_price, row.quote.no_price)
            if spr and row.quote.spread is not None and abs(row.quote.spread) > 0.05:
                prices += f" {spr}"

            if row.is_new:
                pfx = "★ "
            elif is_alert:
                pfx = "⚡ "
            else:
                pfx = "  "

            cat_col = _category_with_emoji(row.meta.category)
            print(
                f"  {_fmt_eta(row.time_to_end_sec):>7}  "
                f"{prices:<11}  "
                f"{delta_str:<7}  "
                f"{cat_col:<20}  "
                f"{pfx}{_truncate(row.meta.question, 50)}"
            )

        hidden = total - min(total, config.TOP_MARKETS_DISPLAY)
        no_liq = total - liq
        if hidden > 0:
            print(f"  + {hidden} não exibidos")
        if no_liq > 0:
            print(f"  {no_liq} sem liquidez")
        print(sep)
