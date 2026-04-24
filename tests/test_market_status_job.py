"""Market-status polling job: writes Dhan status to `settings` for the web UI."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.db import connect, init_db
from app.dhan.client import DhanClient
from app.worker.jobs import market_status_poll_job


BASE = "https://api.dhan.example/v2"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    return tmp_path


@pytest.mark.asyncio
async def test_poll_writes_status_row(state_dir):
    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/marketfeed/marketstatus").mock(
            return_value=httpx.Response(200, json={"status": "open"})
        )
        c = DhanClient(BASE, "cid", "tok")
        await market_status_poll_job(c)
        await c.close()

    conn = connect()
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM settings WHERE key = 'market_status'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["value"] == "OPEN"
    assert row["updated_at"]  # non-empty timestamp


@pytest.mark.asyncio
async def test_poll_does_not_overwrite_on_transport_error(state_dir):
    # Seed a prior value so we can check it's preserved on failure.
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("market_status", "CLOSED", "2026-04-24T09:00:00+05:30"),
        )
    finally:
        conn.close()

    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        # 503 triggers DhanUnavailable; the job should swallow and no-op.
        m.get("/marketfeed/marketstatus").mock(return_value=httpx.Response(503))
        c = DhanClient(BASE, "cid", "tok")
        await market_status_poll_job(c)
        await c.close()

    conn = connect()
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM settings WHERE key = 'market_status'"
        ).fetchone()
    finally:
        conn.close()
    # Row unchanged — stale data naturally times out via _read_market_status.
    assert row["value"] == "CLOSED"
    assert row["updated_at"] == "2026-04-24T09:00:00+05:30"
