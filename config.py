"""
config.py — Centraliza todas as configurações do scanner.
Valores podem ser sobrescritos via variáveis de ambiente ou CLI args.
"""

import os

# ── Janela e intervalo ────────────────────────────────────────────────────────
WINDOW_MINUTES: int = int(os.getenv("WINDOW_MINUTES", "60"))
SCAN_INTERVAL_SEC: int = int(os.getenv("SCAN_INTERVAL_SEC", "60"))

# ── APIs ──────────────────────────────────────────────────────────────────────
GAMMA_BASE: str = "https://gamma-api.polymarket.com"
CLOB_BASE: str = "https://clob.polymarket.com"
POLYMARKET_BASE: str = "https://polymarket.com"

# ── HTTP ──────────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT_SEC: int = 10
MAX_RETRIES: int = 3
BACKOFF_BASE: float = 1.5   # segundos; espera = BACKOFF_BASE ** attempt
MAX_WORKERS: int = 10           # threads para CLOB fallbacks individuais
PAGE_LIMIT: int = 500           # itens por página na Gamma API
LAST_TRADES_BATCH_SIZE: int = 30  # tokens por request GET (evita 414 URI Too Large)

# ── Pricing ───────────────────────────────────────────────────────────────────
# Se |yes_price + no_price - 1| > SPREAD_THRESHOLD → usar last_trade_price
SPREAD_THRESHOLD: float = 0.15
# Delta de preço entre ciclos que dispara destaque visual
PRICE_ALERT_THRESHOLD: float = 0.05

# ── Output ────────────────────────────────────────────────────────────────────
# "console" | "jsonl" | "sqlite" | "all"
OUTPUT_MODE: str = os.getenv("OUTPUT_MODE", "jsonl")
# True = só imprime se houve mudança vs ciclo anterior
PRINT_ONLY_CHANGES: bool = os.getenv("PRINT_ONLY_CHANGES", "false").lower() == "true"
TOP_MARKETS_DISPLAY: int = 50
TOP_CATEGORIES_DISPLAY: int = 5

# ── Timezone de display ───────────────────────────────────────────────────────
DISPLAY_TZ: str = os.getenv("DISPLAY_TZ", "America/Sao_Paulo")

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR: str = "data"
DB_PATH: str = os.path.join(DATA_DIR, "scanner.db")

# ══════════════════════════════════════════════════════════════════════════════
# ── Lending Bot ──────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

# ── Signal Filters ───────────────────────────────────────────────────────────
BOT_MIN_PROBABILITY: float = float(os.getenv("BOT_MIN_PROBABILITY", "0.97"))
BOT_IDEAL_PROBABILITY: float = 0.99
BOT_MAX_MINUTES_TO_END: int = int(os.getenv("BOT_MAX_MINUTES_TO_END", "360"))
BOT_IDEAL_MINUTES_TO_END: int = 15
BOT_MIN_BOOK_DEPTH_USD: float = float(os.getenv("BOT_MIN_BOOK_DEPTH_USD", "200"))
BOT_MAX_BOOK_SPREAD: float = 0.05

# ── Risk ─────────────────────────────────────────────────────────────────────
BOT_MAX_POSITION_USD: float = float(os.getenv("BOT_MAX_POSITION_USD", "100"))
BOT_MAX_TOTAL_EXPOSURE_USD: float = float(os.getenv("BOT_MAX_TOTAL_EXPOSURE_USD", "500"))
BOT_MAX_CONCURRENT_POSITIONS: int = 10
BOT_MAX_HOURLY_LOSS_USD: float = float(os.getenv("BOT_MAX_HOURLY_LOSS_USD", "50"))
BOT_LOSS_COOLDOWN_SEC: int = 3600

# ── Execution ────────────────────────────────────────────────────────────────
BOT_ORDER_TYPE: str = os.getenv("BOT_ORDER_TYPE", "limit")
BOT_TICK_SIZE: str = "0.01"
BOT_SCAN_INTERVAL_SEC: int = int(os.getenv("BOT_SCAN_INTERVAL_SEC", "15"))

# ── Scoring weights ──────────────────────────────────────────────────────────
BOT_W_PROB: float = 0.40
BOT_W_TIME: float = 0.20
BOT_W_DEPTH: float = 0.20
BOT_W_SPREAD: float = 0.20
