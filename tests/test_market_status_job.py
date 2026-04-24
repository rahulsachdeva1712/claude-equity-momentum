"""Market-status polling job: writes local-clock status to `settings` for the web UI.

The Dhan `/v2/marketfeed/marketstatus` endpoint was retired with no v2
replacement, so `DhanClient.market_status` now derives OPEN/CLOSED from an
IST weekday + market-hours check. See FRD B.5 and the 2026-04-24 B.16 row.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from app.db import connect, init_db
from app.dhan.client import DhanClient
from app.worker.jobs import market_status_poll_job

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    return tmp_path


@pytest.mark.asyncio
async def test_poll_writes_open_during_market_hours(state_dir, monkeypatch):
    # Wednesday 2026-04-22 10:00 IST — mid-session on a weekday.
    fake_now = datetime(2026, 4, 22, 10, 0, tzinfo=IST)
    monkeypatch.setattr("app.time_utils.now_ist", lambda: fake_now)
    monkeypatch.setattr("app.worker.jobs.now_ist", lambda: fake_now)

    c = DhanClient("https://api.dhan.example/v2", "cid", "tok")
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
    assert row["updated_at"] == fake_now.isoformat()


@pytest.mark.asyncio
async def test_poll_writes_closed_outside_market_hours(state_dir, monkeypatch):
    # Wednesday 2026-04-22 18:00 IST — after close on a weekday.
    fake_now = datetime(2026, 4, 22, 18, 0, tzinfo=IST)
    monkeypatch.setattr("app.time_utils.now_ist", lambda: fake_now)
    monkeypatch.setattr("app.worker.jobs.now_ist", lambda: fake_now)

    c = DhanClient("https://api.dhan.example/v2", "cid", "tok")
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
    assert row["value"] == "CLOSED"


@pytest.mark.asyncio
async def test_poll_writes_closed_on_weekend(state_dir, monkeypatch):
    # Saturday 2026-04-25 10:00 IST — weekend, even within clock window.
    fake_now = datetime(2026, 4, 25, 10, 0, tzinfo=IST)
    monkeypatch.setattr("app.time_utils.now_ist", lambda: fake_now)
    monkeypatch.setattr("app.worker.jobs.now_ist", lambda: fake_now)

    c = DhanClient("https://api.dhan.example/v2", "cid", "tok")
    await market_status_poll_job(c)
    await c.close()

    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'market_status'"
        ).fetchone()
    finally:
        conn.close()
    assert row["value"] == "CLOSED"
