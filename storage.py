"""
storage.py — Persistência de snapshots em JSONL e/ou SQLite.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime

import config
from models import MarketRow, ScanResult

log = logging.getLogger(__name__)


# ── Helpers de serialização ────────────────────────────────────────────────────

def _row_to_dict(row: MarketRow) -> dict:
    m = row.meta
    q = row.quote
    return {
        "market_id": m.market_id,
        "condition_id": m.condition_id,
        "question": m.question,
        "slug": m.slug,
        "url": m.url,
        "category": m.category,
        "tags": m.tags,
        "end_date": m.end_date.isoformat(),
        "liquidity": m.liquidity,
        "volume": m.volume,
        "time_to_end_sec": row.time_to_end_sec,
        "is_new": row.is_new,
        "yes_price": q.yes_price,
        "no_price": q.no_price,
        "yes_mid": q.yes_mid,
        "no_mid": q.no_mid,
        "yes_last": q.yes_last,
        "no_last": q.no_last,
        "spread": q.spread,
        "price_source": q.price_source,
        "has_liquidity": q.has_liquidity,
        "price_delta_yes": row.price_delta_yes,
        "price_delta_no": row.price_delta_no,
    }


def _result_to_dict(result: ScanResult) -> dict:
    return {
        "ts": result.scan_ts.isoformat(),
        "cycle": result.cycle_num,
        "window_minutes": result.window_minutes,
        "elapsed_sec": round(result.elapsed_sec, 2),
        "new_count": result.new_count,
        "dropped_count": result.dropped_count,
        "aggregates": {
            "total": len(result.markets),
            "by_category": {cat: len(rows) for cat, rows in result.by_category.items()},
        },
        "markets": [_row_to_dict(r) for r in result.markets],
    }


# ── JSONL ──────────────────────────────────────────────────────────────────────

class JsonlStorage:
    def __init__(self, data_dir: str = config.DATA_DIR) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._data_dir = data_dir
        self._current_path: str | None = None
        self._file = None

    def _path_for_today(self) -> str:
        return os.path.join(self._data_dir, f"snapshots_{date.today():%Y%m%d}.jsonl")

    def write(self, result: ScanResult) -> None:
        path = self._path_for_today()
        # Reabre se mudou de dia
        if path != self._current_path:
            self.flush()
            self._current_path = path
            self._file = open(path, "a", encoding="utf-8")
            log.info("JSONL: escrevendo em %s", path)

        line = json.dumps(_result_to_dict(result), ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()

    def flush(self) -> None:
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None


# ── SQLite ─────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    cycle_num   INTEGER,
    total_markets INTEGER,
    elapsed_sec REAL,
    new_count   INTEGER,
    dropped_count INTEGER
);

CREATE TABLE IF NOT EXISTS markets (
    market_id    TEXT NOT NULL,
    snapshot_id  INTEGER REFERENCES snapshot_runs(id),
    condition_id TEXT,
    question     TEXT,
    slug         TEXT,
    url          TEXT,
    category     TEXT,
    end_date     TEXT,
    liquidity    REAL,
    volume       REAL,
    PRIMARY KEY (market_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS quotes (
    snapshot_id     INTEGER REFERENCES snapshot_runs(id),
    market_id       TEXT,
    yes_price       REAL,
    no_price        REAL,
    spread          REAL,
    price_source    TEXT,
    has_liquidity   INTEGER,
    price_delta_yes REAL,
    price_delta_no  REAL,
    PRIMARY KEY (snapshot_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_quotes_market ON quotes(market_id);
CREATE INDEX IF NOT EXISTS idx_runs_ts ON snapshot_runs(ts);
"""


class SqliteStorage:
    def __init__(self, db_path: str = config.DB_PATH) -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _conn_get(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._conn_get()
        conn.executescript(SCHEMA)
        conn.commit()

    def write(self, result: ScanResult) -> None:
        conn = self._conn_get()
        cur = conn.cursor()

        cur.execute(
            """INSERT INTO snapshot_runs
               (ts, cycle_num, total_markets, elapsed_sec, new_count, dropped_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                result.scan_ts.isoformat(),
                result.cycle_num,
                len(result.markets),
                round(result.elapsed_sec, 2),
                result.new_count,
                result.dropped_count,
            ),
        )
        snap_id = cur.lastrowid

        for row in result.markets:
            m, q = row.meta, row.quote
            cur.execute(
                """INSERT OR REPLACE INTO markets
                   (market_id, snapshot_id, condition_id, question, slug, url,
                    category, end_date, liquidity, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (m.market_id, snap_id, m.condition_id, m.question, m.slug, m.url,
                 m.category, m.end_date.isoformat(), m.liquidity, m.volume),
            )
            cur.execute(
                """INSERT OR REPLACE INTO quotes
                   (snapshot_id, market_id, yes_price, no_price, spread,
                    price_source, has_liquidity, price_delta_yes, price_delta_no)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snap_id, m.market_id, q.yes_price, q.no_price, q.spread,
                 q.price_source, int(q.has_liquidity),
                 row.price_delta_yes, row.price_delta_no),
            )

        conn.commit()

    def flush(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ── Storage composto (console ignora, outros escrevem) ─────────────────────────

class Storage:
    """
    Fachada que delega para JSONL e/ou SQLite conforme OUTPUT_MODE.
    OUTPUT_MODE: "console" | "jsonl" | "sqlite" | "all"
    """

    def __init__(self, mode: str = config.OUTPUT_MODE) -> None:
        self._backends: list[JsonlStorage | SqliteStorage] = []
        m = mode.lower()
        if m in ("jsonl", "all"):
            self._backends.append(JsonlStorage())
        if m in ("sqlite", "all"):
            self._backends.append(SqliteStorage())
        if self._backends:
            log.info("Storage iniciado: %s", mode)
        else:
            log.info("Storage: modo console (sem persistência)")

    def write(self, result: ScanResult) -> None:
        for backend in self._backends:
            try:
                backend.write(result)
            except Exception as exc:
                log.error("Erro ao gravar storage %s: %s", type(backend).__name__, exc)

    def flush(self) -> None:
        for backend in self._backends:
            try:
                backend.flush()
            except Exception:
                pass
