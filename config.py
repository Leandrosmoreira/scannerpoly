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
MAX_WORKERS: int = 10       # threads para CLOB fallbacks individuais
PAGE_LIMIT: int = 500       # itens por página na Gamma API

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
