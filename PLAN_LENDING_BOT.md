# PLAN: Bot de Lending Sintetico — Polymarket

## Conceito

**Lending sintetico** = comprar shares de alta probabilidade (99%+) minutos antes
da resolucao, receber $1.00 por share ao resolver. Lucro = spread ($0.01-$0.05).

```
capital → compra YES@$0.99 → mercado resolve → recebe $1.00 → reinveste
```

O edge real: **detectar quando o mercado diz 99% mas na verdade eh 99.9%+**.
Nao basta comprar qualquer 99% — precisa filtrar por liquidez, tempo, e risco.

---

## Arquitetura

```
scannerpoly/                     (existente)
├── config.py                    (extender com configs do bot)
├── models.py                    (extender com LendingSignal, Position, etc.)
├── gamma_client.py              (existente — discovery de mercados)
├── clob_client.py               (existente — pricing)
├── scanner.py                   (existente — scan loop)
├── formatters.py                (existente — display)
├── storage.py                   (existente — JSONL/SQLite)
│
├── bot/                         (NOVO — modulos do lending bot)
│   ├── __init__.py
│   ├── signal_filter.py         # Filtros de entrada (prob, liquidez, tempo, etc.)
│   ├── book_analyzer.py         # Analise de profundidade do order book
│   ├── executor.py              # Execucao de ordens via py-clob-client
│   ├── position_manager.py      # Tracking de posicoes abertas + redemption
│   ├── risk_manager.py          # Limites de exposicao, loss, cooldown
│   ├── capital_rotator.py       # Rotacao de capital pos-resolucao
│   └── pnl_tracker.py           # P&L em tempo real, metricas, alertas
│
├── bot_runner.py                (NOVO — orquestrador principal do bot)
└── .env                         (NOVO — PRIVATE_KEY, WALLET_ADDRESS)
```

---

## Modulo 1: `config.py` — Novas configuracoes

```python
# ── Lending Bot ──────────────────────────────────────────────────────────────
# Probabilidade minima para considerar entrada
BOT_MIN_PROBABILITY: float = 0.97
# Probabilidade ideal (maior confianca)
BOT_IDEAL_PROBABILITY: float = 0.99
# Tempo maximo ate resolucao (minutos) para considerar entrada
BOT_MAX_MINUTES_TO_END: int = 60
# Tempo ideal (minutos) — mais curto = melhor APY anualizado
BOT_IDEAL_MINUTES_TO_END: int = 15
# Liquidez minima no book (USD) do lado que vai comprar
BOT_MIN_BOOK_DEPTH_USD: float = 500.0
# Spread maximo aceitavel no book (ask - bid)
BOT_MAX_BOOK_SPREAD: float = 0.03
# ── Risco ────────────────────────────────────────────────────────────────────
# Capital maximo por trade (USD)
BOT_MAX_POSITION_USD: float = 100.0
# Capital maximo total exposto (USD)
BOT_MAX_TOTAL_EXPOSURE_USD: float = 500.0
# Maximo de posicoes simultaneas
BOT_MAX_CONCURRENT_POSITIONS: int = 10
# Loss maximo por hora antes de cooldown (USD)
BOT_MAX_HOURLY_LOSS_USD: float = 50.0
# Cooldown apos atingir loss limit (segundos)
BOT_LOSS_COOLDOWN_SEC: int = 3600
# ── Execucao ─────────────────────────────────────────────────────────────────
# Tipo de ordem: "limit" (GTC) ou "market" (FOK)
BOT_ORDER_TYPE: str = "limit"
# Slippage maximo para market orders
BOT_MAX_SLIPPAGE: float = 0.005
# Tick size padrao
BOT_TICK_SIZE: str = "0.01"
# Intervalo do scan do bot (segundos)
BOT_SCAN_INTERVAL_SEC: int = 15
# Heartbeat interval (segundos) — CLOB exige a cada 10s
BOT_HEARTBEAT_INTERVAL_SEC: int = 5
```

---

## Modulo 2: `models.py` — Novos dataclasses

```python
@dataclass
class LendingSignal:
    """Sinal de entrada detectado pelo filtro."""
    market_id: str
    condition_id: str
    question: str
    token_id: str           # token do lado vencedor (YES ou NO)
    side: str               # "YES" ou "NO"
    probability: float      # preco atual (ex: 0.99)
    spread: float           # spread do book
    book_depth_usd: float   # profundidade do lado da compra
    time_to_end_sec: int    # segundos ate endDate
    expected_roi: float     # (1 - prob) / prob
    annualized_apy: float   # ROI anualizado
    score: float            # score composto para ranking
    tick_size: str          # "0.01" ou "0.001"
    neg_risk: bool          # True se multi-outcome market
    detected_at: datetime

@dataclass
class BotPosition:
    """Posicao aberta pelo bot."""
    position_id: str        # UUID interno
    market_id: str
    condition_id: str
    token_id: str
    side: str               # "YES" ou "NO"
    entry_price: float
    size: float             # quantidade de shares
    cost_usd: float         # entry_price * size
    order_id: str           # ID da ordem no CLOB
    order_status: str       # "pending" | "filled" | "partial" | "cancelled"
    fill_price: float | None
    fill_size: float | None
    entered_at: datetime
    expected_resolve_at: datetime
    resolved: bool
    payout: float | None    # $1.00 * fill_size se ganhou, 0 se perdeu
    pnl: float | None       # payout - cost

@dataclass
class BotState:
    """Estado global do bot."""
    total_capital: float
    available_capital: float
    total_exposure: float
    positions: list[BotPosition]
    total_pnl: float
    hourly_pnl: float
    trades_count: int
    wins: int
    losses: int
    is_cooldown: bool
    cooldown_until: datetime | None
```

---

## Modulo 3: `signal_filter.py` — Filtros de Entrada

O core da estrategia. Nao eh so comprar 99% — eh filtrar QUANDO o 99% eh na
verdade 99.9%+.

```
Entradas do scanner (MarketRow[])
        │
        ▼
┌─────────────────────────────┐
│  Filtro 1: Probabilidade    │  prob >= BOT_MIN_PROBABILITY
│  (YES >= 0.97 OU NO >= 0.97)│
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Filtro 2: Tempo            │  time_to_end <= BOT_MAX_MINUTES_TO_END
│  (< 60 min ate resolver)   │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Filtro 3: Liquidez         │  book_depth >= BOT_MIN_BOOK_DEPTH_USD
│  (profundidade do book)     │  book_spread <= BOT_MAX_BOOK_SPREAD
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Filtro 4: Risco            │  nao esta em cooldown
│  (exposure limits OK)       │  exposure < max
│                             │  posicoes < max concorrente
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Filtro 5: Duplicata        │  nao tem posicao aberta
│  (ja entrou nesse market?)  │  nesse market
└──────────┬──────────────────┘
           │
           ▼
    LendingSignal[]
    (ordenados por score)
```

### Scoring (ranking dos sinais)

```python
score = (
    w_prob * normalize(probability, 0.97, 1.0)        # quanto mais alto melhor
  + w_time * normalize(1/time_to_end, ...)             # quanto menos tempo melhor
  + w_depth * normalize(book_depth, 500, 10000)        # quanto mais profundo melhor
  + w_spread * normalize(1/spread, ...)                # quanto menor spread melhor
)
```

Pesos sugeridos: `w_prob=0.4, w_time=0.2, w_depth=0.2, w_spread=0.2`

---

## Modulo 4: `book_analyzer.py` — Analise de Order Book

```python
class BookAnalyzer:
    def analyze(self, token_id: str) -> BookAnalysis:
        """
        GET /book?token_id=...
        Analisa:
        - Profundidade total do lado BUY (asks para quem quer comprar)
        - Spread (melhor ask - melhor bid)
        - Slippage estimado para tamanho da posicao desejada
        - Detecta walls (grandes ordens que indicam manipulacao)
        """

@dataclass
class BookAnalysis:
    best_bid: float
    best_ask: float
    spread: float
    depth_bid_usd: float    # total USD nos bids
    depth_ask_usd: float    # total USD nos asks
    slippage_100: float     # slippage para comprar $100
    slippage_500: float     # slippage para comprar $500
    has_wall: bool           # wall detectado (ordem > 5x mediana)
    estimated_fill_price: float  # preco medio ponderado
```

---

## Modulo 5: `executor.py` — Execucao de Ordens

Usa `py-clob-client` oficial para autenticacao e ordens.

```python
class Executor:
    def __init__(self):
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("PK"),
            chain_id=137,
            signature_type=0,
            funder=os.getenv("WALLET_ADDRESS"),
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def buy_limit(self, signal: LendingSignal, size_usd: float) -> str:
        """
        Coloca ordem limit GTC.
        Retorna order_id.
        """
        size_shares = size_usd / signal.probability
        resp = self.client.create_and_post_order(
            OrderArgs(
                token_id=signal.token_id,
                price=signal.probability,
                size=size_shares,
                side=BUY,
            ),
            options={"tick_size": signal.tick_size, "neg_risk": signal.neg_risk},
            order_type=OrderType.GTC,
        )
        return resp["orderID"]

    def buy_market(self, signal: LendingSignal, amount_usd: float) -> str:
        """
        Coloca ordem market FOK.
        Retorna order_id.
        """
        mo = MarketOrderArgs(
            token_id=signal.token_id,
            amount=amount_usd,
            side=BUY,
            order_type=OrderType.FOK,
        )
        signed = self.client.create_market_order(mo)
        resp = self.client.post_order(signed, OrderType.FOK)
        return resp["orderID"]

    def cancel(self, order_id: str) -> bool:
        """Cancela ordem."""

    def get_order_status(self, order_id: str) -> dict:
        """Verifica status da ordem."""

    def heartbeat_loop(self):
        """
        Thread separada que envia heartbeat a cada 5s.
        SEM HEARTBEAT = todas as ordens sao canceladas automaticamente.
        """
```

### Dependencia

```bash
pip install py-clob-client
```

---

## Modulo 6: `position_manager.py` — Tracking de Posicoes

```python
class PositionManager:
    def open_position(self, signal: LendingSignal, order_id: str, size_usd: float):
        """Registra nova posicao."""

    def update_fill(self, position_id: str, fill_price: float, fill_size: float):
        """Atualiza com dados de fill."""

    def check_resolutions(self):
        """
        Para cada posicao aberta:
        1. Verifica se mercado resolveu (via Gamma API: market.resolved == True)
        2. Se resolveu, calcula payout:
           - Ganhou: payout = fill_size * $1.00
           - Perdeu: payout = $0.00
        3. Atualiza P&L
        """

    def get_open_positions(self) -> list[BotPosition]:
        """Lista posicoes abertas."""

    def get_total_exposure(self) -> float:
        """Soma cost_usd de todas posicoes abertas."""
```

### Redemption

A redemption de tokens eh on-chain via CTF contract. Para a v1 do bot,
o approach mais simples:

1. Bot detecta que mercado resolveu
2. Marca posicao como resolvida e calcula P&L
3. **Redemption manual via Polymarket UI** (o usuario resgata no site)

Para v2 (automatizado):
```python
# Usando web3.py diretamente no CTF contract
ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
tx = ctf.functions.redeemPositions(
    USDC_ADDRESS,                    # collateral
    bytes(32),                       # parentCollectionId = 0x00...
    condition_id,                    # conditionId do market
    [1, 2],                          # indexSets para ambos outcomes
).build_transaction({...})
```

---

## Modulo 7: `risk_manager.py` — Gerenciamento de Risco

**Este eh o modulo mais importante.** Um unico loss a 99% apaga 99 wins.

```python
class RiskManager:
    def can_trade(self) -> tuple[bool, str]:
        """
        Verifica TODOS os limites antes de entrar:
        1. Capital disponivel >= custo da posicao
        2. Exposure total < BOT_MAX_TOTAL_EXPOSURE_USD
        3. Posicoes abertas < BOT_MAX_CONCURRENT_POSITIONS
        4. Nao esta em cooldown (loss limit nao foi atingido)
        5. Hourly P&L > -BOT_MAX_HOURLY_LOSS_USD
        """

    def size_position(self, signal: LendingSignal) -> float:
        """
        Kelly Criterion simplificado:
        - prob >= 0.99: usa ate BOT_MAX_POSITION_USD
        - prob 0.97-0.99: escala linear (menos capital)

        NUNCA mais que 1/10 do capital total por trade.
        """

    def register_loss(self, amount: float):
        """Registra loss e verifica cooldown."""

    def register_win(self, amount: float):
        """Registra win."""
```

### Tabela de risco

```
Probabilidade | Risco de loss | Posicao sugerida | Observacao
─────────────┼──────────────┼──────────────────┼──────────────────────
   >= 0.999  |    ~0.1%     | ate MAX_POSITION | alta confianca
   0.99      |    ~1%       | 80% do max       | padrao
   0.98      |    ~2%       | 50% do max       | cauteloso
   0.97      |    ~3%       | 30% do max       | minimo aceito
   < 0.97    |    alto      | NAO ENTRAR       | fora do escopo
```

### Matematica do risco

Se prob = 0.99 e size = $100:
- 99 trades OK: lucro = 99 * $1.01 = +$99.99
- 1 trade loss: perda = 1 * $99 = -$99.00
- Net apos 100 trades: +$0.99

**O edge so existe se a prob real > prob implicita.**

Se prob real = 0.999 mas mercado diz 0.99:
- 999 trades OK: lucro = 999 * $1.01 = +$1009.99
- 1 trade loss: perda = 1 * $99 = -$99.00
- Net apos 1000 trades: +$910.99

---

## Modulo 8: `pnl_tracker.py` — Metricas e Alertas

```python
class PnLTracker:
    # Metricas em tempo real
    total_pnl: float
    hourly_pnl: float
    daily_pnl: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    sharpe_ratio: float      # risk-adjusted return
    max_drawdown: float

    # Alertas
    def check_alerts(self):
        """
        Alerta se:
        - Loss > threshold
        - Win rate caindo
        - Drawdown > 5%
        - Posicao presa (nao resolveu em tempo esperado)
        """

    # Persistencia
    def save_trade(self, position: BotPosition):
        """Salva em JSONL para analise posterior."""

    def print_dashboard(self):
        """Imprime P&L no terminal com rich."""
```

---

## Modulo 9: `bot_runner.py` — Orquestrador

```python
class LendingBot:
    """
    Loop principal do bot de lending sintetico.

    Ciclo a cada BOT_SCAN_INTERVAL_SEC:
    1. Scanner busca mercados proximos de resolver
    2. signal_filter filtra por probabilidade, liquidez, risco
    3. book_analyzer verifica profundidade em tempo real
    4. risk_manager aprova ou rejeita
    5. executor coloca ordem
    6. position_manager tracked
    7. Repete

    Thread paralela:
    - heartbeat_loop (a cada 5s)
    - resolution_checker (a cada 30s, verifica se mercados resolveram)
    """

    def run(self):
        # Thread: heartbeat
        Thread(target=self.executor.heartbeat_loop, daemon=True).start()
        # Thread: resolution checker
        Thread(target=self.resolution_loop, daemon=True).start()

        while not self._stop:
            cycle_start = time.time()

            # 1. Scan
            markets = self.scanner.scan_once()

            # 2. Filtrar sinais
            signals = self.signal_filter.filter(markets)

            # 3. Para cada sinal (ordenado por score)
            for signal in signals:
                # 3a. Verificar risco
                can, reason = self.risk_manager.can_trade()
                if not can:
                    log.info("Skip: %s", reason)
                    break

                # 3b. Analisar book em tempo real
                book = self.book_analyzer.analyze(signal.token_id)
                if not book.is_tradeable:
                    continue

                # 3c. Calcular tamanho
                size = self.risk_manager.size_position(signal)

                # 3d. Executar
                order_id = self.executor.buy_limit(signal, size)
                self.position_manager.open_position(signal, order_id, size)

                log.info("ENTRADA: %s | %s@%.3f | $%.2f",
                         signal.side, signal.question[:40], signal.probability, size)

            # 4. Dashboard
            self.pnl_tracker.print_dashboard()

            # 5. Sleep compensado
            elapsed = time.time() - cycle_start
            sleep_time = max(0, BOT_SCAN_INTERVAL_SEC - elapsed)
            time.sleep(sleep_time)
```

---

## Fluxo de Dados Completo

```
                  ┌──────────────┐
                  │  Gamma API   │  Discovery de mercados
                  └──────┬───────┘
                         │
                  ┌──────▼───────┐
                  │   Scanner    │  Markets com endDate proximos
                  └──────┬───────┘
                         │
                  ┌──────▼───────┐
                  │ CLOB Pricing │  Precos YES/NO via midpoints/last-trades
                  └──────┬───────┘
                         │
                  ┌──────▼───────┐
                  │Signal Filter │  prob >= 0.97, tempo OK, liquidez OK
                  └──────┬───────┘
                         │
                  ┌──────▼───────┐
                  │Book Analyzer │  Profundidade real, slippage, walls
                  └──────┬───────┘
                         │
                  ┌──────▼───────┐
                  │Risk Manager  │  Exposure OK, loss limit OK, sizing
                  └──────┬───────┘
                         │
                  ┌──────▼───────┐
                  │  Executor    │  py-clob-client → POST /order
                  │  (+ heartbeat│  heartbeat a cada 5s
                  └──────┬───────┘
                         │
                  ┌──────▼───────┐
                  │  Position    │  Track fills, check resolutions
                  │  Manager     │  Calcula P&L
                  └──────┬───────┘
                         │
                  ┌──────▼───────┐
                  │ P&L Tracker  │  Dashboard, alertas, historico
                  └──────────────┘
```

---

## Setup e Requisitos

### Dependencias novas

```bash
pip install py-clob-client web3
```

### Variaveis de ambiente (.env)

```env
# Polygon wallet private key (SEM 0x prefix)
PRIVATE_KEY=abc123...
# Wallet address
WALLET_ADDRESS=0x...
# Capital inicial (para tracking)
BOT_INITIAL_CAPITAL=1000
```

### Pre-requisitos na wallet

1. Ter USDC.e na Polygon (nao USDC nativo)
2. Ter MATIC para gas (poucos centavos por tx)
3. Aprovar allowances dos contratos:
   - CTF Exchange: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
   - Neg Risk CTF Exchange: `0xC5d563A36AE78145C45a50134d48A1215220f80a`

### Como rodar

```bash
# Modo dry-run (simula sem executar ordens reais)
python bot_runner.py --dry-run

# Modo real
python bot_runner.py

# Customizado
python bot_runner.py --min-prob 0.99 --max-position 200 --max-exposure 1000
```

---

## Fases de Implementacao

### Fase 1: Signal Detection (sem execucao)
- [ ] Extender scanner para calcular probabilidade (max(yes, no))
- [ ] Implementar signal_filter.py com todos os filtros
- [ ] Implementar book_analyzer.py
- [ ] Log de sinais detectados (sem comprar)
- [ ] Validar que os sinais fazem sentido em dry-run

### Fase 2: Execution Engine
- [ ] Setup py-clob-client com autenticacao
- [ ] Implementar executor.py (limit + market orders)
- [ ] Heartbeat loop
- [ ] Testes com ordens pequenas ($1-5)

### Fase 3: Position Management
- [ ] Tracking de posicoes abertas
- [ ] Resolution checker (Gamma API polling)
- [ ] Calculo de P&L
- [ ] Persistencia em JSONL/SQLite

### Fase 4: Risk Management
- [ ] Limites de exposicao
- [ ] Kelly sizing
- [ ] Loss cooldown
- [ ] Alertas

### Fase 5: Dashboard e Metricas
- [ ] Terminal dashboard com rich (P&L, posicoes, win rate)
- [ ] Historico de trades
- [ ] Metricas de performance

### Fase 6: Redemption Automatica (Opcional)
- [ ] Integracao web3.py com CTF contract
- [ ] Auto-redeem apos resolucao

---

## Riscos Criticos — LEIA ANTES DE RODAR

| Risco | Impacto | Mitigacao |
|-------|---------|-----------|
| Black swan (99% falha) | Loss total da posicao | Kelly sizing, max 1/10 do capital |
| Oracle delay | Capital preso por horas | Timeout, nao alocar tudo |
| Disputa UMA | Capital preso 48h+ | Diversificar mercados |
| Liquidez falsa | Fill ruim ou nao consegue sair | Book analyzer pre-trade |
| Fill parcial | Menos exposure que esperado | Aceitar partial, ajustar size |
| Manipulacao whale | Preco cai apos entrada | Stop-loss mental, max position |
| Smart contract bug | Perda de fundos | Usar contratos auditados |
| API rate limit | Ordens nao entram | Respeitar limites, backoff |
| Heartbeat falha | Todas ordens canceladas | Thread dedicada, retry |

### Regra de ouro
> **Nunca arrisque dinheiro que nao pode perder.**
> Comece com $10-50 para validar a estrategia antes de escalar.
