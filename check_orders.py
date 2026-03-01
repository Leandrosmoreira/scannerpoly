"""
check_orders.py — Utilitario para verificar ordens e trades na Polymarket.

Uso:
    python check_orders.py              # Saldo + status de cada ordem local
    python check_orders.py --order ID   # Consulta uma ordem especifica
    python check_orders.py --all        # Mostra tudo
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


def _init_executor():
    """Inicializa Executor em modo live (somente leitura)."""
    from bot.executor import Executor
    ex = Executor(dry_run=False)
    return ex


def show_balance(ex):
    """Mostra saldo USDC."""
    print("\n  === SALDO ===")
    try:
        balance = ex.get_balance_usdc()
        print(f"  USDC disponivel: ${balance:,.2f}")
    except Exception as exc:
        print(f"  ERRO ao consultar saldo: {exc}")
    print()


def show_open_orders(ex):
    """Lista ordens abertas no CLOB."""
    print("  === ORDENS ABERTAS (CLOB API) ===")
    try:
        orders = ex.get_open_orders()
        if not orders:
            print("  Nenhuma ordem aberta no CLOB.")
        else:
            print(f"  {len(orders)} ordens abertas:")
            for o in orders:
                oid = o.get("id") or o.get("orderID", "?")
                status = o.get("status", "?")
                side = o.get("side", "?")
                price = o.get("price", "?")
                size = o.get("original_size") or o.get("size", "?")
                filled = o.get("size_matched", "0")
                print(f"    {status:>10}  {side:>4}  ${price}  size={size}  filled={filled}  id={str(oid)[:24]}")
    except Exception as exc:
        print(f"  ERRO: {exc}")
    print()


def show_trades(ex):
    """Lista trades recentes (fills)."""
    print("  === TRADES / FILLS (CLOB API) ===")
    try:
        trades = ex.get_trades()
        if not trades:
            print("  Nenhum trade encontrado.")
        else:
            print(f"  {len(trades)} trades:")
            for t in trades[:20]:
                status = t.get("status", "?")
                side = t.get("side", "?")
                price = t.get("price", "?")
                size = t.get("size", "?")
                ts = t.get("created_at") or t.get("timestamp", "?")
                print(f"    {status:>10}  {side:>4}  ${price}  size={size}  {str(ts)[:19]}")
    except Exception as exc:
        print(f"  ERRO: {exc}")
    print()


def show_order(ex, order_id: str):
    """Consulta uma ordem especifica."""
    print(f"\n  === ORDEM {order_id[:30]}... ===")
    try:
        order = ex.get_order(order_id)
        if not order:
            print("  Ordem nao encontrada (resposta vazia).")
        else:
            print(json.dumps(order, indent=2, ensure_ascii=False))
    except Exception as exc:
        print(f"  ERRO: {exc}")
    print()


def check_local_positions(ex):
    """
    Le posicoes do positions.jsonl e consulta status REAL de cada ordem no CLOB.
    Isso mostra se a ordem foi filled, cancelled, etc.
    """
    pos_file = os.path.join(config.DATA_DIR, "positions.jsonl")
    if not os.path.exists(pos_file):
        print("\n  Nenhuma posicao local (positions.jsonl nao existe).\n")
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
        print("\n  Nenhuma posicao local.\n")
        return

    # Agrupar por position_id (pegar ultima versao de cada)
    by_id: dict[str, dict] = {}
    for p in positions:
        pid = p.get("position_id", "")
        by_id[pid] = p

    unique = list(by_id.values())
    print(f"\n  === POSICOES LOCAIS ({len(unique)}) — verificando status real no CLOB ===\n")

    for p in unique:
        side = p.get("side", "?")
        question = p.get("question", "?")[:50]
        entry = p.get("entry_price", 0)
        cost = p.get("cost_usd", 0)
        oid = p.get("order_id", "")
        local_status = p.get("order_status", "?")

        print(f"  {side:>3} @ {entry:.3f}  ${cost:.2f}  {question}")
        print(f"  Local status: {local_status}")
        print(f"  Order ID: {oid}")

        # Consultar CLOB para status real
        if oid and not oid.startswith("DRY_"):
            try:
                order_data = ex.get_order(oid)
                if order_data:
                    clob_status = order_data.get("status", "?")
                    size_matched = order_data.get("size_matched", "0")
                    orig_size = order_data.get("original_size") or order_data.get("size", "?")
                    price = order_data.get("price", "?")
                    created = order_data.get("created_at") or order_data.get("timestamp", "?")

                    print(f"  CLOB status: {clob_status}")
                    print(f"  CLOB size: {orig_size} | filled: {size_matched} | price: {price}")
                    print(f"  Created: {str(created)[:19]}")

                    # Determinar o que aconteceu
                    if clob_status in ("matched", "MATCHED"):
                        print(f"  >>> ORDEM FOI PREENCHIDA (filled)")
                    elif clob_status in ("cancelled", "CANCELLED"):
                        print(f"  >>> ORDEM FOI CANCELADA (provavelmente heartbeat parou)")
                    elif clob_status in ("live", "LIVE"):
                        print(f"  >>> ORDEM ATIVA no book")
                    else:
                        print(f"  >>> Status: {clob_status}")
                        # Dump completo para debug
                        print(f"  Raw: {json.dumps(order_data, indent=4)}")
                else:
                    print(f"  CLOB: resposta vazia (ordem pode ter expirado)")
            except Exception as exc:
                print(f"  CLOB ERRO: {exc}")
        else:
            print(f"  (dry-run, nao consulta CLOB)")

        print()


def main():
    parser = argparse.ArgumentParser(description="Verifica ordens e trades na Polymarket")
    parser.add_argument("--order", type=str, help="ID de uma ordem especifica")
    parser.add_argument("--balance", action="store_true", help="Mostra saldo USDC")
    parser.add_argument("--all", action="store_true", help="Mostra tudo")
    parser.add_argument("--local", action="store_true", help="Mostra posicoes locais (sem consultar CLOB)")
    parser.add_argument("--trades", action="store_true", help="Mostra trades/fills")
    args = parser.parse_args()

    ex = _init_executor()

    if args.order:
        show_order(ex, args.order)
        return

    if args.all:
        show_balance(ex)
        show_open_orders(ex)
        show_trades(ex)
        check_local_positions(ex)
        return

    if args.balance:
        show_balance(ex)
        return

    if args.local:
        # Sem consulta ao CLOB
        pos_file = os.path.join(config.DATA_DIR, "positions.jsonl")
        if os.path.exists(pos_file):
            with open(pos_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            p = json.loads(line)
                            print(f"  {p.get('side','?'):>3} @ {p.get('entry_price',0):.3f}  "
                                  f"${p.get('cost_usd',0):.2f}  {p.get('question','?')[:45]}  "
                                  f"[{p.get('order_status','?')}]")
                        except json.JSONDecodeError:
                            continue
        return

    if args.trades:
        show_trades(ex)
        return

    # Default: saldo + verificar cada posicao local no CLOB
    show_balance(ex)
    check_local_positions(ex)


if __name__ == "__main__":
    main()
