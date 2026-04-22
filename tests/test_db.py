from app.db import connect, init_db


def test_init_db_creates_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    conn = connect()
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in (
        "settings",
        "sessions",
        "signals",
        "paper_orders",
        "paper_fills",
        "paper_book",
        "paper_pnl_daily",
        "live_orders",
        "live_fills",
        "live_positions_snapshot",
        "live_pnl_daily",
        "alerts",
        "audit_log",
    ):
        assert t in names, f"missing {t}"
    conn.close()


def test_wal_mode_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    conn = connect()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()


def test_correlation_id_unique(tmp_path, monkeypatch):
    import pytest
    import sqlite3

    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    conn = connect()
    conn.execute(
        "INSERT INTO live_orders (session_date,symbol,action,order_qty,correlation_id,status,placed_at)"
        " VALUES ('2026-04-22','CDG','BUY',140,'emrb:x','PENDING','2026-04-22T09:30')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO live_orders (session_date,symbol,action,order_qty,correlation_id,status,placed_at)"
            " VALUES ('2026-04-22','CUPID','BUY',1,'emrb:x','PENDING','2026-04-22T09:30')"
        )
    conn.close()
