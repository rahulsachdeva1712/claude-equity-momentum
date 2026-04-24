"""FastAPI smoke tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.web.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_paper_tab_renders(client):
    r = client.get("/paper")
    assert r.status_code == 200
    assert "Paper Trading" in r.text


def test_live_tab_renders(client):
    r = client.get("/live")
    assert r.status_code == 200
    assert "Live Trading" in r.text


def test_top_bar_partial(client):
    r = client.get("/partials/top-bar")
    assert r.status_code == 200
    assert "token:" in r.text
    assert "market:" in r.text


def test_kill_switch_toggle_redirect_and_persists(client):
    # Default: off
    r = client.get("/live")
    assert "Live trading is <strong>OFF</strong>" in r.text or "live: OFF" in r.text

    r = client.post("/settings/live-enabled", data={"enabled": "1"}, follow_redirects=False)
    assert r.status_code == 303

    r = client.get("/paper")
    assert "live: ON" in r.text

    r = client.post("/settings/live-enabled", data={"enabled": "0"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/paper")
    assert "live: OFF" in r.text


def test_root_redirects_to_paper(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/paper" in r.headers["location"]


def test_market_pill_unknown_when_no_row(client):
    # No market_status row written yet → pill renders 'unknown'.
    r = client.get("/partials/top-bar")
    assert r.status_code == 200
    assert "market: unknown" in r.text


def test_market_pill_reflects_fresh_row(client):
    from app.db import connect
    from app.time_utils import now_ist

    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("market_status", "OPEN", now_ist().isoformat()),
        )
    finally:
        conn.close()
    r = client.get("/partials/top-bar")
    assert "market: open" in r.text
    assert "market: unknown" not in r.text


def test_market_pill_unknown_when_row_stale(client):
    from datetime import timedelta

    from app.db import connect
    from app.time_utils import now_ist

    stale_ts = (now_ist() - timedelta(seconds=600)).isoformat()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("market_status", "OPEN", stale_ts),
        )
    finally:
        conn.close()
    r = client.get("/partials/top-bar")
    assert "market: unknown" in r.text


def test_ack_alert(client):
    # Seed an alert via DB
    from app.db import connect
    from app.alerts import Alert, raise_alert

    conn = connect()
    aid = raise_alert(conn, Alert(severity="warn", source="test", message="boo"))
    conn.close()

    r = client.post(f"/alerts/{aid}/ack", follow_redirects=False)
    assert r.status_code == 303

    conn = connect()
    row = conn.execute("SELECT acknowledged_at FROM alerts WHERE id = ?", (aid,)).fetchone()
    conn.close()
    assert row["acknowledged_at"] is not None
