"""
pnl_tracker.py — Logging de sinais e metricas para o dry-run (Fase 1).

Em dry-run, nao executa ordens — apenas loga sinais detectados e
simula P&L teorico para validar a estrategia.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import config
from models import LendingSignal

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


class PnLTracker:
    """Rastreia sinais detectados e P&L teorico."""

    def __init__(self, live: bool = False) -> None:
        self.live = live
        self.signals_logged: int = 0
        self.theoretical_pnl: float = 0.0
        self.theoretical_trades: int = 0
        self._log_path = os.path.join(config.DATA_DIR, "lending_signals.jsonl")
        # Dedup: nao loga o mesmo market_id mais de uma vez por sessao
        self._seen_market_ids: set[str] = set()
        Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)

    def log_signal(self, signal: LendingSignal) -> bool:
        """
        Loga sinal detectado em JSONL e simula trade.
        Retorna True se logado, False se duplicata (mesmo market nesta sessao).
        """
        if signal.market_id in self._seen_market_ids:
            return False

        self._seen_market_ids.add(signal.market_id)
        self.signals_logged += 1

        # Simula trade teorico
        position_usd = min(config.BOT_MAX_POSITION_USD, 100.0)
        shares = position_usd / signal.probability
        gross_pnl = shares * (1.0 - signal.probability)
        fee = position_usd * 0.001  # 0.1% taker fee
        net_pnl = gross_pnl - fee

        self.theoretical_pnl += net_pnl
        self.theoretical_trades += 1

        # JSONL
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market_id": signal.market_id,
            "question": signal.question,
            "side": signal.side,
            "probability": signal.probability,
            "opposite_prob": signal.opposite_prob,
            "time_to_end_sec": signal.time_to_end_sec,
            "expected_roi": signal.expected_roi,
            "annualized_apy": signal.annualized_apy,
            "score": signal.score,
            "book_depth_usd": signal.book_depth_usd,
            "spread": signal.spread,
            "category": signal.category,
            "url": signal.url,
            "sim_position_usd": round(position_usd, 2),
            "sim_shares": round(shares, 2),
            "sim_gross_pnl": round(gross_pnl, 4),
            "sim_net_pnl": round(net_pnl, 4),
        }

        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Falha ao escrever signal log: %s", exc)

        return True

    def print_summary(self, signals: list[LendingSignal], cycle: int) -> None:
        """Imprime resumo dos sinais no terminal."""
        if _RICH:
            self._print_rich(signals, cycle)
        else:
            self._print_plain(signals, cycle)

    # ── Rich ─────────────────────────────────────────────────────────────────

    def _print_rich(self, signals: list[LendingSignal], cycle: int) -> None:
        mode = "LIVE 🔴" if self.live else "DRY RUN"
        _console.print()
        _console.rule(
            f"[bold magenta]LENDING BOT — {mode}[/]  "
            f"[dim]ciclo #{cycle}[/]  "
            f"[white]{len(signals)} sinais[/]",
            style="dim magenta",
        )

        if not signals:
            _console.print("  [dim]Nenhum sinal detectado neste ciclo[/]")
            self._print_stats_rich()
            _console.print()
            return

        table = Table(
            box=box.SIMPLE_HEAD,
            show_edge=False,
            pad_edge=True,
            expand=False,
            header_style="bold dim",
        )
        table.add_column("SCORE", width=6, justify="right")
        table.add_column("PROB",  width=7, justify="right")
        table.add_column("SIDE",  width=4, justify="center")
        table.add_column("ETA",   width=7, justify="right")
        table.add_column("ROI",   width=7, justify="right")
        table.add_column("APY",   width=9, justify="right")
        table.add_column("DEPTH", width=8, justify="right")
        table.add_column("SPREAD", width=7, justify="right")
        table.add_column("MERCADO", min_width=30)

        for s in signals[:15]:
            # Score color
            if s.score >= 0.7:
                score_style = "bold green"
            elif s.score >= 0.4:
                score_style = "yellow"
            else:
                score_style = "dim"

            # Prob color
            if s.probability >= 0.99:
                prob_style = "bold green"
            elif s.probability >= 0.98:
                prob_style = "green"
            else:
                prob_style = "yellow"

            # ETA
            eta = s.time_to_end_sec
            m, sec = divmod(eta, 60)
            if m >= 60:
                h, m = divmod(m, 60)
                eta_str = f"{h}h{m:02d}m"
            else:
                eta_str = f"{m}m{sec:02d}s"

            # Side color
            side_style = "green" if s.side == "YES" else "red"

            # APY format
            apy = s.annualized_apy
            if apy >= 1000:
                apy_str = f"{apy/1000:.0f}k%"
            elif apy >= 100:
                apy_str = f"{apy:.0f}%"
            else:
                apy_str = f"{apy:.1f}%"

            is_new = s.market_id not in self._seen_market_ids
            new_marker = "[bold green]★[/] " if is_new else "[dim]·[/]  "

            question = s.question
            if len(question) > 43:
                question = question[:42] + "…"

            table.add_row(
                Text(f"{s.score:.2f}", style=score_style),
                Text(f"{s.probability:.3f}", style=prob_style),
                Text(s.side, style=side_style),
                Text(eta_str, style="bold white"),
                Text(f"{s.expected_roi:.3f}", style="cyan"),
                Text(apy_str, style="magenta"),
                Text(f"${s.book_depth_usd:,.0f}", style="dim"),
                Text(f"{s.spread:.4f}" if s.spread else "—", style="dim"),
                Text.from_markup(f"{new_marker}{question}"),
            )

        _console.print(table)
        self._print_stats_rich()
        _console.print()

    def _print_stats_rich(self) -> None:
        pnl_style = "green" if self.theoretical_pnl >= 0 else "red"
        _console.print(
            f"  [dim]Sinais totais:[/] [bold]{self.signals_logged}[/]"
            f"  [dim]·[/]  "
            f"[dim]Trades simulados:[/] [bold]{self.theoretical_trades}[/]"
            f"  [dim]·[/]  "
            f"[dim]P&L teorico:[/] [{pnl_style}]${self.theoretical_pnl:+.2f}[/]"
        )

    # ── Plain ────────────────────────────────────────────────────────────────

    def _print_plain(self, signals: list[LendingSignal], cycle: int) -> None:
        mode = "LIVE" if self.live else "DRY RUN"
        W = 100
        print(f"\n{'=' * W}")
        print(f"  LENDING BOT — {mode}  |  ciclo #{cycle}  |  {len(signals)} sinais")
        print(f"{'─' * W}")

        if not signals:
            print("  Nenhum sinal detectado neste ciclo")
        else:
            print(f"  {'SCORE':>6}  {'PROB':>7}  {'SIDE':>4}  {'ETA':>7}  "
                  f"{'ROI':>7}  {'DEPTH':>8}  MERCADO")
            print(f"  {'─'*6}  {'─'*7}  {'─'*4}  {'─'*7}  {'─'*7}  {'─'*8}  {'─'*40}")
            for s in signals[:15]:
                eta = s.time_to_end_sec
                m, sec = divmod(eta, 60)
                eta_str = f"{m}m{sec:02d}s"
                q = s.question[:40] if len(s.question) > 40 else s.question
                print(
                    f"  {s.score:6.2f}  {s.probability:7.3f}  {s.side:>4}  "
                    f"{eta_str:>7}  {s.expected_roi:7.3f}  "
                    f"${s.book_depth_usd:>7,.0f}  {q}"
                )

        pnl_sign = "+" if self.theoretical_pnl >= 0 else ""
        print(f"\n  Sinais: {self.signals_logged}  |  "
              f"Trades sim: {self.theoretical_trades}  |  "
              f"P&L teorico: {pnl_sign}${self.theoretical_pnl:.2f}")
        print(f"{'=' * W}")
