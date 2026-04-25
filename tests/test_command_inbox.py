"""Command inbox: the 'Run rebalance now' path from UI click to worker pickup."""
from __future__ import annotations

import os
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.dhan.client import DhanClient
from app.paths import command_inbox
from app.time_utils import now_ist, session_date_for
from app.web.main import create_app
from app.worker.jobs import (
    COMMAND_MAX_AGE_SECONDS,
    COMMAND_RUN_REBALANCE,
    command_inbox_job,
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    return tmp_path


@pytest.fixture
def client(state_dir):
    app = create_app()
    with TestClient(app) as c:
        yield c


def _count_alerts(source: str) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE source = ?", (source,)
        ).fetchone()["c"]
    finally:
        conn.close()


def test_web_endpoint_drops_sentinel(client):
    r = client.post("/actions/run-rebalance", follow_redirects=False)
    assert r.status_code == 303
    inbox = command_inbox()
    assert (inbox / COMMAND_RUN_REBALANCE).exists()


def test_web_endpoint_redirects_to_referer(client):
    r = client.post(
        "/actions/run-rebalance",
        headers={"referer": "http://testserver/live"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "http://testserver/live"


@pytest.mark.asyncio
async def test_inbox_job_rejects_after_completion(state_dir, monkeypatch):
    # Mark today's *trading* session as already executed — the rerun must be
    # refused. Use ``session_date_for`` (matches the production guard) so this
    # passes on weekends and holidays where today's IST date rolls back to the
    # last trading day.
    sess = session_date_for(now_ist())
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO sessions (session_date, execution_completed_at, market_open)"
            " VALUES (?, ?, 1)",
            (sess.isoformat(), now_ist().isoformat()),
        )
    finally:
        conn.close()

    inbox = command_inbox()
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / COMMAND_RUN_REBALANCE).touch()

    # Spy: replace execution_job so we can prove the worker did NOT run it.
    call_count = {"n": 0}

    async def _no_run(*_a, **_kw):
        call_count["n"] += 1

    monkeypatch.setattr("app.worker.jobs.execution_job", _no_run)

    dhan = DhanClient("https://api.dhan.example/v2", "cid", "tok")
    await command_inbox_job(dhan)
    await dhan.close()

    assert call_count["n"] == 0
    assert not (inbox / COMMAND_RUN_REBALANCE).exists()
    # The UI disables the Refresh button once today's execution is complete;
    # a sentinel that slips through (stale file / race) is silently dropped —
    # no alert, to keep the pill from filling up with expected rejections.
    assert _count_alerts("commands") == 0


@pytest.mark.asyncio
async def test_inbox_job_discards_stale_command(state_dir, monkeypatch):
    inbox = command_inbox()
    inbox.mkdir(parents=True, exist_ok=True)
    p = inbox / COMMAND_RUN_REBALANCE
    p.touch()
    # Backdate well past the stale threshold.
    old_ts = time.time() - (COMMAND_MAX_AGE_SECONDS + 60)
    os.utime(p, (old_ts, old_ts))

    call_count = {"n": 0}

    async def _no_run(*_a, **_kw):
        call_count["n"] += 1

    monkeypatch.setattr("app.worker.jobs.execution_job", _no_run)

    dhan = DhanClient("https://api.dhan.example/v2", "cid", "tok")
    await command_inbox_job(dhan)
    await dhan.close()

    assert call_count["n"] == 0
    assert not p.exists()


@pytest.mark.asyncio
async def test_inbox_job_happy_path_invokes_execution(state_dir, monkeypatch):
    inbox = command_inbox()
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / COMMAND_RUN_REBALANCE).touch()

    call_count = {"n": 0}

    async def _fake_exec(*_a, **_kw):
        call_count["n"] += 1

    monkeypatch.setattr("app.worker.jobs.execution_job", _fake_exec)

    dhan = DhanClient("https://api.dhan.example/v2", "cid", "tok")
    await command_inbox_job(dhan)
    await dhan.close()

    assert call_count["n"] == 1
    assert not (inbox / COMMAND_RUN_REBALANCE).exists()


@pytest.mark.asyncio
async def test_inbox_job_handles_unknown_command(state_dir):
    inbox = command_inbox()
    inbox.mkdir(parents=True, exist_ok=True)
    weird = inbox / "bogus.now"
    weird.touch()

    dhan = DhanClient("https://api.dhan.example/v2", "cid", "tok")
    await command_inbox_job(dhan)
    await dhan.close()

    assert not weird.exists()
    assert _count_alerts("commands") >= 1
