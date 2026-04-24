"""SQLite schema + connection helper. FRD B.3.

Single writer invariant: only the worker process mutates tables other than
`settings`. Web writes to `settings` and to a small command inbox on disk.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.paths import db_file

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_date TEXT PRIMARY KEY,
    execution_completed_at TEXT,
    market_open INTEGER
);

CREATE TABLE IF NOT EXISTS signals (
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    security_id TEXT,
    exchange_segment TEXT,
    selected INTEGER NOT NULL,
    rank_by_126d INTEGER,
    target_weight REAL,
    target_qty INTEGER,
    reference_price REAL,
    PRIMARY KEY (session_date, symbol)
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,            -- BUY / TRIM / EXIT
    order_qty INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,            -- PENDING / FILLED / SKIPPED
    note TEXT
);

CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_order_id INTEGER NOT NULL REFERENCES paper_orders(id) ON DELETE CASCADE,
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,              -- BUY / SELL
    fill_qty INTEGER NOT NULL,
    fill_price REAL NOT NULL,
    charges_total REAL NOT NULL,
    charges_json TEXT NOT NULL,
    filled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_book (
    symbol TEXT PRIMARY KEY,
    qty INTEGER NOT NULL,
    avg_cost REAL NOT NULL,
    cost_basis REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_pnl_daily (
    session_date TEXT PRIMARY KEY,
    realized REAL NOT NULL,
    unrealized REAL NOT NULL,
    mtm REAL NOT NULL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    order_qty INTEGER NOT NULL,
    correlation_id TEXT NOT NULL UNIQUE,
    dhan_order_id TEXT,
    status TEXT NOT NULL,            -- PENDING / TRANSIT / OPEN / TRADED / REJECTED / CANCELLED
    reject_reason TEXT,
    placed_at TEXT NOT NULL,
    terminal_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_live_orders_correlation ON live_orders(correlation_id);
CREATE INDEX IF NOT EXISTS idx_live_orders_session ON live_orders(session_date);

CREATE TABLE IF NOT EXISTS live_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    live_order_id INTEGER NOT NULL REFERENCES live_orders(id) ON DELETE CASCADE,
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    fill_qty INTEGER NOT NULL,
    fill_price REAL NOT NULL,
    charges_total REAL NOT NULL,
    charges_json TEXT NOT NULL,
    filled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_positions_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL,
    avg_cost REAL NOT NULL,
    ltp REAL,
    unrealized REAL,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_taken ON live_positions_snapshot(taken_at);

CREATE TABLE IF NOT EXISTS live_pnl_daily (
    session_date TEXT PRIMARY KEY,
    realized REAL NOT NULL,
    unrealized REAL NOT NULL,
    mtm REAL NOT NULL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL,          -- info / warn / error / critical
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_unack ON alerts(acknowledged_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    correlation_id TEXT,
    kind TEXT NOT NULL,              -- PLACE / MODIFY / CANCEL / STATUS
    request_json TEXT NOT NULL,
    response_json TEXT,
    http_status INTEGER,
    created_at TEXT NOT NULL
);

-- Last-known mark for open paper-book symbols, so the UI can show true
-- live-MTM per position. Written by a periodic worker job during market
-- hours (single-writer invariant: worker only). Read by the web paper tab.
CREATE TABLE IF NOT EXISTS live_ltp (
    symbol TEXT PRIMARY KEY,
    ltp REAL NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    path: Path = db_file()
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(DDL)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction because isolation_level=None."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
