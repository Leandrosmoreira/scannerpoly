# PLAN.md — Polymarket Close-Within-1h Scanner (revisado)

---

## 1. Objetivo

Scanner em tempo real que lista, a cada 60 segundos, todos os mercados do Polymarket que encerram nos próximos 60 minutos, com pricing atual por outcome e exportação de snapshots para uso posterior por bot/dashboard.

---

## 2. Contexto das APIs (validado)

### 2.1 Gamma API — `https://gamma-api.polymarket.com`

Pública, sem autenticação. Throttling leve; implementar retry.

| Endpoint | Uso |
|---|---|
| `GET /markets` | Lista markets com filtros de data |
| `GET /events` | Lista eventos (contêm N markets) |

Parâmetros relevantes de `/markets`:
- `limit` (int, max 500)
- `offset` (int)
- `active` (bool string `"true"`)
- `closed` (bool string `"false"`)
- `end_date_min` (ISO 8601 UTC, ex: `"2024-01-15T14:00:00Z"`)
- `end_date_max` (ISO 8601 UTC)

Campos do objeto market que importam:
```json
{
  "id": "...",
  "conditionId": "0xabc...",
  "question": "Will X happen?",
  "slug": "will-x-happen",
  "endDate": "2024-01-15T15:00:00Z",
  "category": "Sports",
  "tags": [{"label": "Soccer"}],
  "active": true,
  "closed": false,
  "liquidity": 5200.50,
  "volume": 12000.00,
  "tokens": [
    {"token_id": "abc123", "outcome": "Yes"},
    {"token_id": "def456", "outcome": "No"}
  ]
}
```

**Atenção:** campo `tokens` com outcome `"Yes"`/`"No"` (capitalized). Extrair e normalizar.

### 2.2 CLOB API — `https://clob.polymarket.com`

Pública para leitura de preços, sem autenticação.

| Endpoint | Método | Retorno |
|---|---|---|
| `/midpoint?token_id={id}` | GET | `{"mid": "0.92"}` |
| `/midpoints` | POST body `{"token_ids": [...]}` | `{"mid": {"id1": "0.92", ...}}` |
| `/last-trade-price?token_id={id}` | GET | `{"price": "0.92"}` |
| `/last-trades-prices?token_ids={id1,id2}` | GET | `[{"token_id": "...", "price": "0.92"}]` |
| `/price?token_id={id}&side=BUY` | GET | `{"price": "0.93"}` |
| `/book?token_id={id}` | GET | Full order book |

**Prioridade de pricing (RF-02 revisado):**
1. `POST /midpoints` (bulk — 1 chamada para N tokens) → campo `"mid"`
2. `GET /last-trades-prices` (bulk) → campo `"price"`
3. `GET /price?side=BUY` + `GET /price?side=SELL` → calcular mid manualmente
4. `GET /book` → `(best_bid + best_ask) / 2`

**Threshold de spread aberto:** se `|yes_price - (1 - no_price)| > SPREAD_THRESHOLD` (default `0.15`), sinalizar como "spread largo" e preferir last_trade_price.

**URL pública de mercado:** `https://polymarket.com/event/{slug}`

---

## 3. Requisitos Funcionais (revisados)

### RF-01 — Descoberta de mercados

- Endpoint: `GET /markets`
- Filtros: `active=true`, `closed=false`, `end_date_min=now`, `end_date_max=now+60min`
- Paginação: loop até receber menos itens do que `limit`; usar `limit=500` para minimizar roundtrips
- Markets sem `tokens` ou com `tokens` vazio: ignorar com warning

### RF-02 — Cotação atual (revisado)

Estratégia bulk-first para evitar N chamadas:

```
1. Coletar todos token_ids (YES + NO) do ciclo atual
2. POST /midpoints com lista completa → preencher cache
3. Para tokens sem mid (ou mid=0): GET /last-trades-prices em bulk
4. Para tokens ainda sem preço: GET /price individualmente
5. Sinalizar mercados completamente sem pricing como "sem liquidez"
```

Campos do `MarketQuote`:
- `yes_price`, `no_price` (preço final exibido)
- `yes_mid`, `no_mid` (raw do midpoint)
- `yes_last`, `no_last` (raw do last trade)
- `spread` = `yes_price + no_price - 1.0` (em mercados binários deve ser ≈ 0)
- `price_source` = `"mid" | "last_trade" | "book" | "none"`
- `has_liquidity` = bool

### RF-03 — Loop de atualização

```
while True:
    t0 = time.monotonic()
    run_scan_cycle()
    elapsed = time.monotonic() - t0
    sleep(max(0, SCAN_INTERVAL_SEC - elapsed))  # desconta tempo de execução
```

- Dedupe opcional: comparar `(market_id, yes_price, no_price)` com snapshot anterior; só reemitir se mudou
- Heartbeat: logar a cada ciclo `[HEARTBEAT] cycle #{n}, {len} markets, elapsed {t:.1f}s`

### RF-04 — Agrupamento por categoria

- Agrupar por campo `category` (string). Fallback: primeiro `tag.label` da lista, ou `"Other"`
- Exibir top 5 categorias no cabeçalho do ciclo
- Contagem "encerrando por hora" = `len(soon_markets)` (janela já é 60 min)

### RF-05 — Export

- **JSONL** (padrão): `data/snapshots_YYYYMMDD.jsonl`, uma linha JSON por ciclo
- **SQLite** (opcional): tabelas `snapshot_runs`, `markets`, `quotes`
- Ambos ativados por `OUTPUT_MODE` em config

### RF-06 — Delta tracking (novo)

Entre ciclos consecutivos, detectar e destacar:
- Mercados novos na janela (entraram)
- Mercados saídos (encerramento já passou)
- Price move > `PRICE_ALERT_THRESHOLD` (default `0.05`) em qualquer outcome

---

## 4. Requisitos Não Funcionais (revisados)

### RNF-01 — Robustez

```python
# Política de retry uniforme (tenacity ou manual)
MAX_RETRIES = 3
BACKOFF_BASE = 1.5  # seconds
TIMEOUT_SEC = 10

# Por chamada:
for attempt in range(MAX_RETRIES):
    try:
        resp = session.get(url, timeout=TIMEOUT_SEC)
        resp.raise_for_status()
        return resp.json()
    except (Timeout, ConnectionError, HTTPError) as e:
        if attempt == MAX_RETRIES - 1:
            log.warning(f"Failed after {MAX_RETRIES} retries: {url}")
            return None
        sleep(BACKOFF_BASE ** attempt)
```

### RNF-02 — Concorrência

O CLOB tem fallbacks que podem precisar de chamadas individuais por token. Usar `ThreadPoolExecutor` para paralelizar:

```python
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:  # MAX_WORKERS=10 default
    futures = {pool.submit(fetch_price_fallback, tid): tid for tid in missing_tokens}
```

Evitar abrir mais conexões do que o rate limit suporta; `MAX_WORKERS=10` é conservador.

### RNF-03 — Timezones

- Todas as comparações internas em UTC (`datetime.now(timezone.utc)`)
- Display: UTC + America/Sao_Paulo em paralelo (configurável via `DISPLAY_TZ`)
- Parsear `endDate` via `dateutil.parser.isoparse()` — lida com formatos variados do Gamma

### RNF-04 — Shutdown gracioso (novo)

```python
import signal

def handle_shutdown(sig, frame):
    log.info("Shutdown requested, flushing storage...")
    storage.flush()
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)
```

---

## 5. Estrutura do projeto

```
polymarket_scanner/
├── PLAN.md
├── README.md
├── requirements.txt
├── config.py
├── models.py
├── gamma_client.py
├── clob_client.py
├── scanner.py          # ponto de entrada + orquestrador
├── storage.py
├── formatters.py
├── data/               # criado em runtime
│   └── snapshots_YYYYMMDD.jsonl
├── logs/               # opcional
└── tests/
    ├── __init__.py
    ├── test_parsing.py
    ├── test_clob_fallback.py
    └── conftest.py     # fixtures e mocks HTTP
```

---

## 6. Módulos e responsabilidades (detalhado)

### 6.1 `config.py`

```python
# Janela e intervalo
WINDOW_MINUTES: int = 60
SCAN_INTERVAL_SEC: int = 60

# APIs
GAMMA_BASE: str = "https://gamma-api.polymarket.com"
CLOB_BASE: str = "https://clob.polymarket.com"
POLYMARKET_BASE: str = "https://polymarket.com"

# HTTP
REQUEST_TIMEOUT_SEC: int = 10
MAX_RETRIES: int = 3
BACKOFF_BASE: float = 1.5
MAX_WORKERS: int = 10       # threads para CLOB fallback
PAGE_LIMIT: int = 500       # máximo por página Gamma

# Pricing
SPREAD_THRESHOLD: float = 0.15   # spread aberto → preferir last_trade
PRICE_ALERT_THRESHOLD: float = 0.05  # delta de preço para alertar

# Output
OUTPUT_MODE: str = "jsonl"        # "console" | "jsonl" | "sqlite" | "all"
PRINT_ONLY_CHANGES: bool = False
TOP_MARKETS_DISPLAY: int = 50
TOP_CATEGORIES_DISPLAY: int = 5

# Timezone de display
DISPLAY_TZ: str = "America/Sao_Paulo"

# Paths
DATA_DIR: str = "data"
DB_PATH: str = "data/scanner.db"
```

### 6.2 `models.py`

```python
@dataclass
class MarketMeta:
    market_id: str           # campo "id" do Gamma
    condition_id: str        # campo "conditionId"
    question: str
    slug: str
    url: str                 # construído: POLYMARKET_BASE/event/{slug}
    category: str            # "Other" se ausente
    tags: list[str]
    end_date: datetime       # UTC
    yes_token_id: str
    no_token_id: str
    liquidity: float
    volume: float

@dataclass
class MarketQuote:
    yes_price: float | None  # preço final exibido
    no_price: float | None
    yes_mid: float | None
    no_mid: float | None
    yes_last: float | None
    no_last: float | None
    spread: float | None     # yes_price + no_price - 1.0
    price_source: str        # "mid" | "last_trade" | "book" | "none"
    has_liquidity: bool
    fetched_at: datetime     # UTC

@dataclass
class MarketRow:
    meta: MarketMeta
    quote: MarketQuote
    time_to_end_sec: int     # segundos até endDate
    is_new: bool = False     # entrou neste ciclo
    price_delta_yes: float | None = None   # vs ciclo anterior
    price_delta_no: float | None = None

@dataclass
class ScanResult:
    scan_ts: datetime
    cycle_num: int
    window_minutes: int
    markets: list[MarketRow]
    by_category: dict[str, list[MarketRow]]
    elapsed_sec: float
    new_count: int
    dropped_count: int
```

### 6.3 `gamma_client.py`

```python
class GammaClient:
    def list_markets_ending_soon(
        self, start_ts: datetime, end_ts: datetime
    ) -> list[MarketMeta]:
        """
        Pagina GET /markets com:
          active=true, closed=false
          end_date_min=start_ts.isoformat()
          end_date_max=end_ts.isoformat()

        Descarta markets sem tokens.
        Normaliza outcome "Yes"/"No" para yes_token_id/no_token_id.
        Retorna lista ordenada por endDate ASC.
        """

    def _parse_market(self, raw: dict) -> MarketMeta | None:
        """
        Extrai campos, valida presença de tokens,
        constrói URL, parseia endDate com isoparse().
        Retorna None se inválido (loga warning).
        """

    def _paginate(self, params: dict) -> Iterator[dict]:
        """Itera páginas até resultado vazio."""
```

### 6.4 `clob_client.py`

```python
class ClobClient:
    def get_midpoints_bulk(
        self, token_ids: list[str]
    ) -> dict[str, float]:
        """POST /midpoints. Retorna {token_id: mid_price}."""

    def get_last_trades_bulk(
        self, token_ids: list[str]
    ) -> dict[str, float]:
        """GET /last-trades-prices?token_ids=id1,id2,..."""

    def get_price_individual(
        self, token_id: str, side: str = "BUY"
    ) -> float | None:
        """GET /price?token_id=&side=. Fallback individual."""

    def get_book(self, token_id: str) -> dict | None:
        """GET /book?token_id=. Último recurso."""

    def fetch_quotes(
        self, markets: list[MarketMeta]
    ) -> dict[str, MarketQuote]:
        """
        Orquestra a cadeia de fallback para todos os markets:
        1. bulk midpoints
        2. bulk last-trades para os sem mid
        3. ThreadPoolExecutor para fallbacks individuais
        Retorna {market_id: MarketQuote}
        """
```

### 6.5 `scanner.py`

```python
def run_cycle(
    gamma: GammaClient,
    clob: ClobClient,
    storage: Storage,
    formatter: Formatter,
    prev_result: ScanResult | None,
    cycle_num: int,
) -> ScanResult:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)

    markets_meta = gamma.list_markets_ending_soon(now, now + timedelta(minutes=WINDOW_MINUTES))
    quotes = clob.fetch_quotes(markets_meta)

    rows = build_rows(markets_meta, quotes, now, prev_result)
    by_category = group_by_category(rows)

    result = ScanResult(
        scan_ts=now, cycle_num=cycle_num,
        markets=rows, by_category=by_category,
        elapsed_sec=time.monotonic() - t0,
        ...
    )

    storage.write(result)
    formatter.print(result)
    return result

def main():
    setup_signal_handlers()
    gamma, clob, storage, formatter = init_components()
    prev, cycle = None, 0

    while True:
        t0 = time.monotonic()
        try:
            prev = run_cycle(gamma, clob, storage, formatter, prev, cycle)
        except Exception as e:
            log.error(f"Cycle {cycle} failed: {e}", exc_info=True)
        cycle += 1
        sleep(max(0, SCAN_INTERVAL_SEC - (time.monotonic() - t0)))
```

**CLI args** (via `argparse`):
- `--window` (minutos, default 60)
- `--interval` (segundos, default 60)
- `--output` (`console|jsonl|sqlite|all`)
- `--changes-only` (flag)
- `--tz` (timezone de display)

### 6.6 `storage.py`

**JSONL:**
```json
{"ts": "2024-01-15T14:00:00Z", "cycle": 1, "window_minutes": 60,
 "markets": [...], "aggregates": {"total": 12, "by_category": {...}}}
```

**SQLite schema:**
```sql
CREATE TABLE snapshot_runs (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    cycle_num INTEGER,
    total_markets INTEGER,
    elapsed_sec REAL,
    config_json TEXT
);

CREATE TABLE markets (
    market_id TEXT NOT NULL,
    snapshot_id INTEGER REFERENCES snapshot_runs(id),
    condition_id TEXT,
    question TEXT,
    slug TEXT,
    url TEXT,
    category TEXT,
    end_date TEXT,
    liquidity REAL,
    volume REAL,
    PRIMARY KEY (market_id, snapshot_id)
);

CREATE TABLE quotes (
    snapshot_id INTEGER REFERENCES snapshot_runs(id),
    market_id TEXT,
    yes_price REAL,
    no_price REAL,
    spread REAL,
    price_source TEXT,
    has_liquidity INTEGER,
    price_delta_yes REAL,
    price_delta_no REAL,
    PRIMARY KEY (snapshot_id, market_id)
);

CREATE INDEX idx_quotes_market ON quotes(market_id);
CREATE INDEX idx_runs_ts ON snapshot_runs(ts);
```

### 6.7 `formatters.py`

Formato do cabeçalho por ciclo:
```
══════════════════════════════════════════════════════════════════
[SCAN #5] 2024-01-15 14:00:00 UTC | 14:00:00 BRT
12 mercados encerrando na próxima 1h  |  ciclo em 3.2s
Top categorias: Sports(5)  Politics(3)  Crypto(2)  Other(2)
NEW: 2 entraram | DROPPED: 1 saiu
══════════════════════════════════════════════════════════════════
ETA      YES    NO     SPR   CAT        TITLE
──────────────────────────────────────────────────────────────────
 2m14s   0.93   0.08   0.01  Sports     Mazatlán vs Pachuca
 5m03s   0.61   0.40   0.01  Politics   Eleição GO-2
12m34s   0.72   0.30  ▲0.06  Crypto     BTC > 100k hoje?  ← alert
...
+2 mercados sem liquidez (sem exibição de preço)
──────────────────────────────────────────────────────────────────
```

- `rich` para cores: ETAs < 5min em vermelho, alertas de preço em amarelo, mercados novos com prefixo `★`
- Spread exibido com prefixo `▲`/`▼` quando delta relevante

---

## 7. Testes

### `tests/conftest.py`
- Fixture `mock_gamma_response`: retorna lista de markets sintéticos
- Fixture `mock_clob_midpoints`: retorna dict de mid prices
- Patcher de `requests.Session` para evitar chamadas reais

### `tests/test_parsing.py`
```
test_parse_end_date_iso8601()         # "2024-01-15T15:00:00Z"
test_parse_end_date_with_offset()     # "2024-01-15T12:00:00-03:00"
test_parse_end_date_missing_tz()      # assume UTC, não explodir
test_token_id_extraction_yes_no()     # extrai corretamente do array tokens
test_token_id_extraction_missing()    # market sem tokens → None, não crash
test_market_url_construction()        # POLYMARKET_BASE/event/{slug}
test_category_fallback_to_tag()       # sem category → usa tags[0].label
test_category_fallback_to_other()     # sem nada → "Other"
```

### `tests/test_clob_fallback.py`
```
test_pricing_uses_mid_when_available()
test_pricing_falls_back_to_last_trade()
test_pricing_falls_back_to_price_endpoint()
test_pricing_returns_none_on_all_failure()
test_spread_threshold_triggers_fallback()
test_bulk_midpoints_batches_correctly()
```

---

## 8. Checklist de implementação

- [ ] 1. Estrutura de pastas + `requirements.txt` + `config.py`
- [ ] 2. `models.py` com dataclasses completos
- [ ] 3. `gamma_client.py`: paginação + parsing + filtro de datas
- [ ] 4. `clob_client.py`: bulk midpoints → bulk last-trades → fallback individual
- [ ] 5. `scanner.py`: ciclo único funcional sem loop
- [ ] 6. `formatters.py`: output console básico (sem rich)
- [ ] 7. Testar manualmente 1 ciclo ao vivo
- [ ] 8. Adicionar rich ao formatter
- [ ] 9. `storage.py`: JSONL
- [ ] 10. Loop principal com sleep corrigido + signal handlers
- [ ] 11. Delta tracking entre ciclos (novo/dropped/preço)
- [ ] 12. `storage.py`: SQLite (opcional)
- [ ] 13. CLI args via argparse
- [ ] 14. Testes unitários com mocks

---

## 9. Critérios de pronto (Definition of Done)

- [ ] Roda 30 minutos sem crash
- [ ] A cada minuto exibe: total, lista por ETA, YES/NO price com fonte
- [ ] Markets sem liquidez não travam o ciclo (exibem "—")
- [ ] JSONL gravado e legível (validar com `python -c "import json; [json.loads(l) for l in open('data/snapshots_*.jsonl')]"`)
- [ ] Ctrl+C encerra graciosamente (flush final de storage)
- [ ] Tempo de ciclo < 30s mesmo com 50 markets (bulk + concorrência)

---

## 10. Dependências

```
requests>=2.31.0
rich>=13.7.0
python-dateutil>=2.8.2
# sem dependências adicionais: sqlite3 e concurrent.futures são stdlib
```

> **Nota de autenticação:** todos os endpoints utilizados são públicos. Nenhuma API key é necessária para leitura de preços e mercados. Se futuramente for necessário submeter ordens, a CLOB API requer autenticação L1/L2 via carteira Ethereum — fora do escopo deste scanner.
