"""
clob_client.py — Acesso à CLOB API do Polymarket para obter preços.

Cadeia de fallback por token (bulk-first):
  1. POST /midpoints          (bulk)
  2. GET  /last-trades-prices (bulk)
  3. GET  /price              (individual, ThreadPoolExecutor)
  4. GET  /book               (individual, último recurso)
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

import config
from models import MarketMeta, MarketQuote

log = logging.getLogger(__name__)


class ClobClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ── Ponto de entrada principal ─────────────────────────────────────────────

    def fetch_quotes(self, markets: list[MarketMeta]) -> dict[str, MarketQuote]:
        """
        Orquestra a cadeia de fallback para todos os markets.
        Retorna {market_id: MarketQuote}.

        Estratégia:
          1. Busca midpoints em bulk para todos os tokens.
          2. Identifica tokens que precisam de last_trade:
               - sem mid  →  fallback natural
               - mid existe mas spread do market é aberto  →  precisa de comparação
          3. Busca last_trades em bulk para esses tokens.
          4. Fallbacks individuais para tokens ainda sem preço.
          5. Monta MarketQuote, preferindo last_trade quando spread > threshold.
        """
        if not markets:
            return {}

        fetched_at = datetime.now(timezone.utc)

        all_token_ids = list({m.yes_token_id for m in markets} | {m.no_token_id for m in markets})

        # ── Passo 1: bulk midpoints ────────────────────────────────────────────
        mid_prices: dict[str, float] = self.get_midpoints_bulk(all_token_ids)

        # ── Passo 2: determinar quais tokens precisam de last_trade ───────────
        need_last: set[str] = set()
        for tid in all_token_ids:
            if tid not in mid_prices:
                need_last.add(tid)
        # Também busca last_trade para markets com spread aberto (para poder comparar)
        for m in markets:
            yes_mid = mid_prices.get(m.yes_token_id)
            no_mid = mid_prices.get(m.no_token_id)
            if yes_mid is not None and no_mid is not None:
                if abs(yes_mid + no_mid - 1.0) > config.SPREAD_THRESHOLD:
                    need_last.add(m.yes_token_id)
                    need_last.add(m.no_token_id)

        # ── Passo 3: bulk last-trades ──────────────────────────────────────────
        last_prices: dict[str, float] = {}
        if need_last:
            last_prices = self.get_last_trades_bulk(list(need_last))

        # ── Passo 4: fallbacks individuais para tokens ainda sem preço ────────
        still_missing = [
            tid for tid in all_token_ids
            if tid not in mid_prices and tid not in last_prices
        ]
        individual: dict[str, tuple[float, str]] = {}
        if still_missing:
            individual = self._fetch_individual_parallel(still_missing)

        if still_missing and len(individual) < len(still_missing):
            truly_missing = [t for t in still_missing if t not in individual]
            log.debug("Sem preço para %d tokens após todos os fallbacks", len(truly_missing))

        # ── Passo 5: montar MarketQuote ────────────────────────────────────────
        quotes: dict[str, MarketQuote] = {}
        src_priority = ["mid", "last_trade", "price_ep", "book", "none"]

        for m in markets:
            yes_mid = mid_prices.get(m.yes_token_id)
            no_mid = mid_prices.get(m.no_token_id)
            yes_last = last_prices.get(m.yes_token_id)
            no_last = last_prices.get(m.no_token_id)
            yes_ind = individual.get(m.yes_token_id)
            no_ind = individual.get(m.no_token_id)

            # Spread calculado a partir dos mids (se ambos disponíveis)
            spread_from_mid: float | None = None
            if yes_mid is not None and no_mid is not None:
                spread_from_mid = yes_mid + no_mid - 1.0

            # Decidir preço final e fonte
            use_last = (
                spread_from_mid is not None
                and abs(spread_from_mid) > config.SPREAD_THRESHOLD
                and yes_last is not None
                and no_last is not None
            )

            if use_last:
                yes_price: float | None = yes_last
                no_price: float | None = no_last
                dominant_src = "last_trade"
            elif yes_mid is not None and no_mid is not None:
                yes_price = yes_mid
                no_price = no_mid
                dominant_src = "mid"
            elif yes_last is not None or no_last is not None:
                yes_price = yes_last
                no_price = no_last
                dominant_src = "last_trade"
            else:
                yes_price = yes_ind[0] if yes_ind else None
                no_price = no_ind[0] if no_ind else None
                yes_src_i = yes_ind[1] if yes_ind else "none"
                no_src_i = no_ind[1] if no_ind else "none"
                dominant_src = min(
                    [yes_src_i, no_src_i],
                    key=lambda s: src_priority.index(s) if s in src_priority else 99,
                )

            spread: float | None = None
            if yes_price is not None and no_price is not None:
                spread = round(yes_price + no_price - 1.0, 4)

            quotes[m.market_id] = MarketQuote(
                yes_price=yes_price,
                no_price=no_price,
                yes_mid=yes_mid,
                no_mid=no_mid,
                yes_last=yes_last,
                no_last=no_last,
                spread=spread,
                price_source=dominant_src,
                has_liquidity=(yes_price is not None and no_price is not None),
                fetched_at=fetched_at,
            )

        return quotes

    # ── Bulk endpoints ─────────────────────────────────────────────────────────

    def get_midpoints_bulk(self, token_ids: list[str]) -> dict[str, float]:
        """POST /midpoints → {token_id: mid_price}."""
        if not token_ids:
            return {}
        try:
            resp = self._session.post(
                config.CLOB_BASE + "/midpoints",
                json={"token_ids": token_ids},
                timeout=config.REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            # Resposta esperada: {"mid": {"token_id": "0.92", ...}}
            raw = data.get("mid") or data
            result: dict[str, float] = {}
            for tid, val in raw.items():
                try:
                    f = float(val)
                    if f > 0:
                        result[tid] = f
                except (TypeError, ValueError):
                    pass
            log.debug("Midpoints bulk: %d/%d tokens com preço", len(result), len(token_ids))
            return result
        except Exception as exc:
            log.warning("Midpoints bulk falhou: %s", exc)
            return {}

    def get_last_trades_bulk(self, token_ids: list[str]) -> dict[str, float]:
        """
        GET /last-trades-prices em batches de LAST_TRADES_BATCH_SIZE tokens.
        Necessário porque URLs muito longas retornam 414 Request-URI Too Large.
        """
        if not token_ids:
            return {}
        result: dict[str, float] = {}
        batch_size = config.LAST_TRADES_BATCH_SIZE
        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i : i + batch_size]
            result.update(self._get_last_trades_page(batch))
        log.debug("Last trades bulk: %d/%d tokens com preço", len(result), len(token_ids))
        return result

    def _get_last_trades_page(self, token_ids: list[str]) -> dict[str, float]:
        """Busca last-trades para um batch de tokens."""
        try:
            resp = self._session.get(
                config.CLOB_BASE + "/last-trades-prices",
                params={"token_ids": ",".join(token_ids)},
                timeout=config.REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            result: dict[str, float] = {}
            if isinstance(data, list):
                for item in data:
                    tid = item.get("token_id", "")
                    val = item.get("price")
                    if tid and val is not None:
                        try:
                            f = float(val)
                            if f > 0:
                                result[tid] = f
                        except (TypeError, ValueError):
                            pass
            elif isinstance(data, dict):
                for tid, val in data.items():
                    try:
                        f = float(val)
                        if f > 0:
                            result[tid] = f
                    except (TypeError, ValueError):
                        pass
            return result
        except Exception as exc:
            log.warning("Last trades bulk falhou: %s", exc)
            return {}

    # ── Fallbacks individuais ─────────────────────────────────────────────────

    def _fetch_individual_parallel(
        self, token_ids: list[str]
    ) -> dict[str, tuple[float, str]]:
        """
        Busca preço individual para cada token_id em paralelo.
        Retorna {token_id: (price, source)}.
        """
        results: dict[str, tuple[float, str]] = {}
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
            futures = {pool.submit(self._fetch_one, tid): tid for tid in token_ids}
            for future in as_completed(futures):
                tid = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        results[tid] = result
                except Exception as exc:
                    log.debug("Fallback individual falhou para %s: %s", tid, exc)
        return results

    def _fetch_one(self, token_id: str) -> tuple[float, str] | None:
        """Tenta /price (BUY+SELL) e depois /book para um token."""
        # Tenta GET /price BUY e SELL para calcular mid
        buy = self._get_price(token_id, "BUY")
        sell = self._get_price(token_id, "SELL")
        if buy is not None and sell is not None:
            return ((buy + sell) / 2, "price_ep")
        if buy is not None:
            return (buy, "price_ep")

        # Fallback final: book
        book = self.get_book(token_id)
        if book:
            mid = self._mid_from_book(book)
            if mid is not None:
                return (mid, "book")

        return None

    def get_price_individual(self, token_id: str, side: str = "BUY") -> float | None:
        return self._get_price(token_id, side)

    def _get_price(self, token_id: str, side: str) -> float | None:
        """GET /price?token_id=&side=."""
        try:
            resp = self._session.get(
                config.CLOB_BASE + "/price",
                params={"token_id": token_id, "side": side},
                timeout=config.REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            val = data.get("price")
            if val is not None:
                f = float(val)
                return f if f > 0 else None
        except Exception:
            pass
        return None

    def get_book(self, token_id: str) -> dict | None:
        """GET /book?token_id= — último recurso."""
        try:
            resp = self._session.get(
                config.CLOB_BASE + "/book",
                params={"token_id": token_id},
                timeout=config.REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    @staticmethod
    def _mid_from_book(book: dict) -> float | None:
        """Calcula mid a partir do melhor bid/ask do book."""
        try:
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            best_bid = max((float(b["price"]) for b in bids), default=None)
            best_ask = min((float(a["price"]) for a in asks), default=None)
            if best_bid is not None and best_ask is not None:
                return (best_bid + best_ask) / 2
            return best_bid or best_ask
        except Exception:
            return None

    # ── HTTP helper com retry ──────────────────────────────────────────────────

    def _get_with_retry(self, path: str, params: dict | None = None) -> dict | list | None:
        url = config.CLOB_BASE + path
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self._session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SEC)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                log.warning("Timeout CLOB %s (tentativa %d)", url, attempt + 1)
            except requests.exceptions.HTTPError as exc:
                if exc.response.status_code in (400, 404):
                    return None
                log.warning("HTTP %s em %s", exc.response.status_code, url)
            except requests.exceptions.RequestException as exc:
                log.warning("Erro rede CLOB %s: %s", url, exc)

            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.BACKOFF_BASE ** attempt)

        return None
