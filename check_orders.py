"""
check_orders.py — Utilitario para verificar ordens e trades na Polymarket.

Uso:
    python check_orders.py              # Lista ordens abertas + trades recentes
    python check_orders.py --order ID   # Consulta uma ordem especifica
    python check_orders.py --balance    # Mostra saldo USDC
    python check_orders.py --all        # Mostra tudo (ordens + trades + saldo)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("check_orders")

# Garantir import dos modulos locais
sys.path.insert(0, os.path.dirname(__file__))

import config

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    _RICH = True
    _console = Console()
except ImportError:
    _RICH = False
    _console = None


def _init_executor():
    """Inicializa Executor em modo live (somente leitura)."""
    from bot.executor import Executor
    ex = Executor(dry_run=False)
    return ex


def show_open_orders(ex):
    """Lista ordens abertas no CLOB."""
    orders = ex.get_open_orders()

    if not orders:
        print("\n  Nenhuma ordem aberta encontrada.\n")
        return

    print(f"\n  === ORDENS ABERTAS ({len(orders)}) ===\n")

    if _RICH:
        table = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=False)
        table.add_column("STATUS", width=10)
        table.add_column("SIDE", width=5)
        table.add_column("PRICE", width=8, justify="right")
        table.add_column("SIZE", width=8, justify="right")
        table.add_column("FILLED", width=8, justify="right")
        table.add_column("ORDER ID", width=20)
        table.add_column("ASSET", width=20)
        table.add_column("CREATED", width=20)

        for o in orders:
            status = o.get("status", "?")
            side = o.get("side", "?")
            price = o.get("price", "?")
            size = o.get("original_size") or o.get("size", "?")
            filled = o.get("size_matched", "0")
            oid = o.get("id") or o.get("orderID", "?")
            asset = o.get("asset_id", "?")
            if isinstance(asset, str) and len(asset) > 18:
                asset = asset[:16] + "..."
            created = o.get("created_at") or o.get("timestamp", "?")

            table.add_row(
                status, side, str(price), str(size), str(filled),
                oid[:18] + "..." if len(str(oid)) > 18 else str(oid),
                asset, str(created)[:19],
            )

        _console.print(table)
    else:
        for o in orders:
            print(f"  {o.get('status', '?'):>10}  {o.get('side', '?'):>4}  "
                  f"${o.get('price', '?'):>6}  size={o.get('original_size', '?'):>6}  "
                  f"filled={o.get('size_matched', 0):>6}  "
                  f"id={str(o.get('id', o.get('orderID', '?')))[:20]}")

    print()


def show_trades(ex):
    """Lista trades recentes (fills)."""
    trades = ex.get_trades()

    if not trades:
        print("\n  Nenhum trade (fill) encontrado.\n")
        return

    print(f"\n  === TRADES / FILLS ({len(trades)}) ===\n")

    if _RICH:
        table = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=False)
        table.add_column("STATUS", width=10)
        table.add_column("SIDE", width=5)
        table.add_column("PRICE", width=8, justify="right")
        table.add_column("SIZE", width=8, justify="right")
        table.add_column("TRADE ID", width=20)
        table.add_column("ORDER ID", width=20)
        table.add_column("TIMESTAMP", width=20)

        for t in trades[:20]:
            status = t.get("status", "?")
            side = t.get("side", "?")
            price = t.get("price", "?")
            size = t.get("size", "?")
            tid = t.get("id") or t.get("tradeID", "?")
            oid = t.get("order_id") or t.get("orderID", "?")
            ts = t.get("created_at") or t.get("timestamp", "?")

            table.add_row(
                status, side, str(price), str(size),
                str(tid)[:18] + "..." if len(str(tid)) > 18 else str(tid),
                str(oid)[:18] + "..." if len(str(oid)) > 18 else str(oid),
                str(ts)[:19],
            )

        _console.print(table)
    else:
        for t in trades[:20]:
            print(f"  {t.get('status', '?'):>10}  {t.get('side', '?'):>4}  "
                  f"${t.get('price', '?'):>6}  size={t.get('size', '?'):>6}  "
                  f"id={str(t.get('id', '?'))[:20]}")

    print()


def show_order(ex, order_id: str):
    """Consulta uma ordem especifica."""
    order = ex.get_order(order_id)

    if not order:
        print(f"\n  Ordem {order_id} nao encontrada.\n")
        return

    print(f"\n  === ORDEM {order_id[:20]}... ===\n")
    print(json.dumps(order, indent=2, ensure_ascii=False))
    print()


def show_balance(ex):
    """Mostra saldo USDC."""
    balance = ex.get_balance_usdc()
    print(f"\n  Saldo USDC: ${balance:,.2f}\n")


def show_local_positions():
    """Mostra posicoes salvas localmente em positions.jsonl."""
    pos_file = os.path.join(config.DATA_DIR, "positions.jsonl")
    if not os.path.exists(pos_file):
        print("\n  Nenhuma posicao local encontrada (positions.jsonl nao existe).\n")
        return

    positions = []
    with open(pos_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    positions.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not positions:
        print("\n  Nenhuma posicao local encontrada.\n")
        return

    # Agrupar por position_id (pegar ultima versao de cada)
    by_id: dict[str, dict] = {}
    for p in positions:
        pid = p.get("position_id", "")
        by_id[pid] = p  # ultima versao sobrescreve

    unique = list(by_id.values())
    print(f"\n  === POSICOES LOCAIS ({len(unique)}) ===\n")

    for p in unique:
        status = p.get("order_status", "?")
        resolved = p.get("resolved", False)
        won = p.get("won")
        pnl = p.get("pnl")
        side = p.get("side", "?")
        question = p.get("question", "?")[:45]
        entry = p.get("entry_price", 0)
        cost = p.get("cost_usd", 0)
        oid = p.get("order_id", "?")

        result = ""
        if resolved:
            if won:
                result = f" WIN  pnl=${pnl:+.2f}" if pnl is not None else " WIN"
            else:
                result = f" LOSS pnl=${pnl:+.2f}" if pnl is not None else " LOSS"
        else:
            result = f" [{status}]"

        print(f"  {side:>3} @ {entry:.3f} ${cost:.2f}  {question}{result}")
        print(f"       order: {str(oid)[:40]}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Verifica ordens e trades na Polymarket")
    parser.add_argument("--order", type=str, help="ID de uma ordem especifica para consultar")
    parser.add_argument("--balance", action="store_true", help="Mostra saldo USDC")
    parser.add_argument("--all", action="store_true", help="Mostra tudo (ordens + trades + saldo + local)")
    parser.add_argument("--local", action="store_true", help="Mostra posicoes salvas localmente")
    parser.add_argument("--trades", action="store_true", help="Mostra trades/fills recentes")
    args = parser.parse_args()

    ex = _init_executor()

    if args.order:
        show_order(ex, args.order)
        return

    if args.all:
        show_balance(ex)
        show_open_orders(ex)
        show_trades(ex)
        show_local_positions()
        return

    if args.balance:
        show_balance(ex)
        return

    if args.local:
        show_local_positions()
        return

    if args.trades:
        show_trades(ex)
        return

    # Default: ordens abertas + saldo
    show_balance(ex)
    show_open_orders(ex)
    show_local_positions()


if __name__ == "__main__":
    main()
