"""
test_order.py — Coloca uma ordem limite de teste (preco baixo, nao executa)
e verifica se aparece no CLOB. Cancela no final.

Uso:
    python test_order.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test_order")

sys.path.insert(0, os.path.dirname(__file__))

import config


def main():
    print("\n" + "=" * 70)
    print("  TESTE DE ORDEM — Polymarket CLOB")
    print("=" * 70)

    # ── 1. Inicializar executor ─────────────────────────────────────────────
    print("\n[1] Inicializando executor...")
    from bot.executor import Executor
    ex = Executor(dry_run=False)
    print("    OK — executor live inicializado")

    # ── 2. Verificar saldo ──────────────────────────────────────────────────
    print("\n[2] Verificando saldo...")
    balance = ex.get_balance_usdc()
    print(f"    Saldo USDC: ${balance:,.2f}")

    if balance < 1:
        print("    ERRO: saldo insuficiente para teste")
        return

    # ── 3. Buscar um mercado ativo para teste ───────────────────────────────
    print("\n[3] Buscando mercado ativo...")
    from datetime import datetime, timedelta, timezone
    from gamma_client import GammaClient

    gamma = GammaClient()
    now = datetime.now(timezone.utc)
    markets = gamma.list_markets_ending_soon(now, now + timedelta(hours=24))

    if not markets:
        print("    ERRO: nenhum mercado encontrado")
        return

    # Pegar primeiro mercado com tokens validos
    market = markets[0]
    print(f"    Mercado: {market.question[:60]}")
    print(f"    Market ID: {market.market_id}")
    print(f"    YES token: {market.yes_token_id[:20]}...")
    print(f"    NO token:  {market.no_token_id[:20]}...")
    print(f"    neg_risk: {market.neg_risk}")

    # ── 4. Buscar preco atual ───────────────────────────────────────────────
    print("\n[4] Buscando preco atual...")
    from clob_client import ClobClient
    clob = ClobClient()
    quotes = clob.fetch_quotes([market])
    quote = quotes.get(market.market_id)

    if quote:
        print(f"    YES price: {quote.yes_price}")
        print(f"    NO price:  {quote.no_price}")
    else:
        print("    Sem cotacao — usando preco de teste")

    # ── 5. Colocar ordem limite a preco MUITO baixo ─────────────────────────
    # Comprar YES a $0.01 — nunca vai executar (mercado teria que desabar)
    test_price = 0.01
    test_size = 5.0  # 5 shares minimo
    test_token = market.yes_token_id

    print(f"\n[5] Colocando ordem de TESTE...")
    print(f"    BUY YES @ ${test_price} x {test_size} shares = ${test_price * test_size:.2f}")
    print(f"    Token: {test_token[:30]}...")
    print(f"    neg_risk: {market.neg_risk}")

    try:
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY

        resp = ex._client.create_and_post_order(
            OrderArgs(
                token_id=test_token,
                price=test_price,
                size=test_size,
                side=BUY,
            ),
            PartialCreateOrderOptions(
                tick_size=config.BOT_TICK_SIZE,
                neg_risk=market.neg_risk,
            ),
        )

        print(f"\n    RESPOSTA COMPLETA:")
        print(f"    {json.dumps(resp, indent=4)}")

        order_id = resp.get("orderID", "")
        status = resp.get("status", "")
        print(f"\n    Order ID: {order_id}")
        print(f"    Status:   {status}")

        if status == "live":
            print("\n    >>> ORDEM ESTA NO BOOK! Funciona!")
        elif status == "matched":
            print("\n    >>> ORDEM FOI EXECUTADA (improvavel a $0.01)")
        else:
            print(f"\n    >>> Status inesperado: {status}")

    except Exception as exc:
        print(f"\n    ERRO ao colocar ordem: {exc}")
        import traceback
        traceback.print_exc()
        return

    # ── 6. Verificar no CLOB ────────────────────────────────────────────────
    if order_id:
        print(f"\n[6] Verificando ordem no CLOB...")
        time.sleep(2)

        try:
            order_data = ex.get_order(order_id)
            print(f"    CLOB response:")
            print(f"    {json.dumps(order_data, indent=4)}")
        except Exception as exc:
            print(f"    Erro ao consultar: {exc}")

        # ── 7. Listar ordens abertas ────────────────────────────────────────
        print(f"\n[7] Listando ordens abertas...")
        try:
            orders = ex.get_open_orders()
            print(f"    {len(orders)} ordens abertas")
            for o in orders:
                oid = o.get("id") or o.get("orderID", "?")
                print(f"      {o.get('status', '?'):>10}  ${o.get('price', '?')}  "
                      f"size={o.get('original_size', '?')}  id={str(oid)[:24]}")
        except Exception as exc:
            print(f"    Erro: {exc}")

        # ── 8. Cancelar ─────────────────────────────────────────────────────
        print(f"\n[8] Cancelando ordem de teste...")
        try:
            ex.cancel(order_id)
            print(f"    Ordem cancelada OK")
        except Exception as exc:
            print(f"    Erro ao cancelar: {exc}")

        # Verificar se cancelou
        time.sleep(1)
        try:
            order_after = ex.get_order(order_id)
            print(f"    Status apos cancelar: {order_after.get('status', '?')}")
        except Exception as exc:
            print(f"    Erro: {exc}")

    print(f"\n{'=' * 70}")
    print("  TESTE CONCLUIDO")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
