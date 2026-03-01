"""
gamma_client.py — Acesso à Gamma API do Polymarket.
Responsável por descobrir mercados que encerram dentro de uma janela de tempo.
"""

from __future__ import annotations

import json as _json
import logging
import time
from datetime import datetime, timezone
from typing import Iterator

import requests
from dateutil import parser as dateutil_parser

import config
from models import MarketMeta

log = logging.getLogger(__name__)


class GammaClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ── API pública ────────────────────────────────────────────────────────────

    def list_markets_ending_soon(
        self, start_ts: datetime, end_ts: datetime
    ) -> list[MarketMeta]:
        """
        Retorna markets ativos cujo endDate cai entre start_ts e end_ts (UTC).
        Ordenados por endDate ASC (menor ETA primeiro).
        """
        params = {
            "active": "true",
            "closed": "false",
            "end_date_min": start_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": end_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": config.PAGE_LIMIT,
        }

        results: list[MarketMeta] = []
        for raw in self._paginate(params):
            market = self._parse_market(raw)
            if market is not None:
                results.append(market)

        results.sort(key=lambda m: m.end_date)
        log.info("Gamma: %d markets encontrados na janela", len(results))
        return results

    # ── Internos ───────────────────────────────────────────────────────────────

    def _paginate(self, params: dict) -> Iterator[dict]:
        """Itera todas as páginas de /markets até esgotar resultados."""
        offset = 0
        while True:
            page_params = {**params, "offset": offset}
            data = self._get("/markets", page_params)
            if data is None or not isinstance(data, list) or len(data) == 0:
                break
            for item in data:
                yield item
            if len(data) < config.PAGE_LIMIT:
                break
            offset += config.PAGE_LIMIT

    def _parse_market(self, raw: dict) -> MarketMeta | None:
        """
        Converte um dict bruto da API em MarketMeta.
        Retorna None se o market for inválido (sem tokens, sem endDate).
        """
        try:
            yes_token_id: str | None = None
            no_token_id: str | None = None

            # Formato antigo: tokens: [{token_id, outcome}, ...]
            tokens: list[dict] = raw.get("tokens") or []
            if len(tokens) >= 2:
                yes_token_id = self._extract_token(tokens, "Yes")
                no_token_id = self._extract_token(tokens, "No")

            # Formato real da API: clobTokenIds (JSON string) + outcomes (JSON string)
            if not yes_token_id or not no_token_id:
                clob_raw = raw.get("clobTokenIds")
                outcomes_raw = raw.get("outcomes")
                if clob_raw and outcomes_raw:
                    try:
                        tid_list = _json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
                        out_list = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                        for tid, outcome in zip(tid_list, out_list):
                            o = str(outcome).strip().lower()
                            if o == "yes":
                                yes_token_id = str(tid).strip() or None
                            elif o == "no":
                                no_token_id = str(tid).strip() or None
                    except Exception as exc:
                        log.debug("Falha ao parsear clobTokenIds/outcomes para %s: %s", raw.get("id"), exc)

            if not yes_token_id or not no_token_id:
                log.debug("Market %s ignorado: não encontrou YES/NO tokens", raw.get("id"))
                return None

            end_date_raw = raw.get("endDate") or raw.get("end_date_iso") or raw.get("end_date")
            if not end_date_raw:
                log.debug("Market %s ignorado: sem endDate", raw.get("id"))
                return None

            end_date = self._parse_dt(end_date_raw)
            if end_date is None:
                return None

            # Categoria: campo direto ou primeiro tag
            category = raw.get("category") or ""
            if not category:
                tags_raw = raw.get("tags") or []
                if tags_raw and isinstance(tags_raw[0], dict):
                    category = tags_raw[0].get("label", "")
                elif tags_raw and isinstance(tags_raw[0], str):
                    category = tags_raw[0]
            category = category.strip() or "Other"

            # Tags como lista de strings
            tags_raw = raw.get("tags") or []
            tags: list[str] = []
            for t in tags_raw:
                if isinstance(t, dict):
                    label = t.get("label", "")
                    if label:
                        tags.append(label)
                elif isinstance(t, str) and t:
                    tags.append(t)

            slug = raw.get("slug") or raw.get("market_slug") or raw.get("id", "")
            url = f"{config.POLYMARKET_BASE}/event/{slug}"

            # Detectar neg_risk: mercados multi-outcome (>2 outcomes)
            # ou campo neg_risk explicito da API
            neg_risk = bool(raw.get("neg_risk") or raw.get("negRisk", False))
            if not neg_risk:
                # Verificar pelo numero de outcomes
                try:
                    clob_ids = _json.loads(clob_raw) if isinstance(clob_raw, str) else (clob_raw or [])
                    if len(clob_ids) > 2:
                        neg_risk = True
                except Exception:
                    pass

            return MarketMeta(
                market_id=str(raw.get("id", "")),
                condition_id=str(raw.get("conditionId") or raw.get("condition_id", "")),
                question=str(raw.get("question") or raw.get("title", "")),
                slug=slug,
                url=url,
                category=category,
                tags=tags,
                end_date=end_date,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                neg_risk=neg_risk,
                liquidity=float(raw.get("liquidity") or 0),
                volume=float(raw.get("volume") or 0),
            )
        except Exception as exc:
            log.warning("Erro ao parsear market %s: %s", raw.get("id"), exc)
            return None

    @staticmethod
    def _extract_token(tokens: list[dict], outcome: str) -> str | None:
        """
        Extrai o token_id para o outcome dado.
        Aceita variações: "Yes"/"No", "YES"/"NO", "yes"/"no".
        """
        for t in tokens:
            if isinstance(t, dict):
                o = str(t.get("outcome", "")).strip().lower()
                if o == outcome.lower():
                    return str(t.get("token_id", "")).strip() or None
        return None

    @staticmethod
    def _parse_dt(value: str) -> datetime | None:
        """Parseia string de data para datetime UTC. Retorna None em erro."""
        try:
            dt = dateutil_parser.isoparse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception as exc:
            log.warning("Falha ao parsear data '%s': %s", value, exc)
            return None

    def _get(self, path: str, params: dict | None = None) -> list | dict | None:
        """GET com retry e backoff exponencial."""
        url = config.GAMMA_BASE + path
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self._session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SEC)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                log.warning("Timeout em %s (tentativa %d/%d)", url, attempt + 1, config.MAX_RETRIES)
            except requests.exceptions.HTTPError as exc:
                log.warning("HTTP %s em %s", exc.response.status_code, url)
                if exc.response.status_code in (400, 404):
                    return None  # não faz sentido retentar
            except requests.exceptions.RequestException as exc:
                log.warning("Erro de rede em %s: %s", url, exc)

            if attempt < config.MAX_RETRIES - 1:
                wait = config.BACKOFF_BASE ** attempt
                log.debug("Aguardando %.1fs antes de retentar...", wait)
                time.sleep(wait)

        log.error("Falha definitiva após %d tentativas: %s", config.MAX_RETRIES, url)
        return None
