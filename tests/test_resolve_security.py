"""security_id fallback chain — regression test for the 2026-04-26
leverage bug. _fetch_0930_closes used to look up security_id only in
today's signals row, so EXIT/TRIM orders for held-but-dropped names
returned None price → SKIPPED → book over-deployed.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.db import connect, init_db, tx
from app.worker.jobs import _resolve_security


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    return connect()


def _seed_signal(conn, sd: date, symbol: str, security_id: str | None = "10001", seg: str = "BSE_EQ"):
    with tx(conn):
        conn.execute(
            "INSERT INTO signals (session_date, symbol, selected, target_qty, rank_by_126d,"
            " target_weight, reference_price, security_id, exchange_segment)"
            " VALUES (?, ?, 1, 100, 1, 0.5, 100.0, ?, ?)",
            (sd.isoformat(), symbol, security_id, seg),
        )


def test_resolve_uses_today_signals_when_present(db):
    today = date(2026, 4, 27)
    _seed_signal(db, today, "GUJCOTEX", security_id="42")
    sec, seg = _resolve_security(db, today, "GUJCOTEX")
    assert sec == "42"
    assert seg == "BSE_EQ"


def test_resolve_falls_back_to_prior_signals_for_held_dropped_name(db):
    """ADANIPOWER traded on 24 Apr but didn't make 27 Apr's top-N.
    `_resolve_security` must still find its security_id from the prior
    signals row so the EXIT can be priced and filled."""
    older = date(2026, 4, 24)
    today = date(2026, 4, 27)
    _seed_signal(db, older, "ADANIPOWER", security_id="999")
    # Today's signals only has GUJCOTEX, no ADANIPOWER row.
    _seed_signal(db, today, "GUJCOTEX", security_id="42")

    sec, seg = _resolve_security(db, today, "ADANIPOWER")
    assert sec == "999"
    assert seg == "BSE_EQ"


def test_resolve_picks_most_recent_prior_when_multiple(db):
    """If multiple prior signals exist, take the most recent one
    (security_ids don't change in practice, but the contract pins the
    behaviour)."""
    _seed_signal(db, date(2026, 3, 1), "X", security_id="aaa")
    _seed_signal(db, date(2026, 4, 10), "X", security_id="bbb")
    _seed_signal(db, date(2026, 4, 20), "X", security_id="ccc")

    sec, _ = _resolve_security(db, date(2026, 4, 27), "X")
    assert sec == "ccc"


def test_resolve_falls_back_to_universe_csv(tmp_path, monkeypatch):
    """When neither today's nor prior signals have a row, the universe
    artifact is the last resort."""
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    conn = connect()

    # Write a minimal universe.csv at the path the resolver looks at.
    from app.universe.refresh import universe_csv_path

    path = universe_csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "symbol,security_id,exchange_segment,isin,sc_code\n"
        "ZZZUNIQ,77777,BSE_EQ,IN0000ZZZ,500999\n",
        encoding="utf-8",
    )

    sec, seg = _resolve_security(conn, date(2026, 4, 27), "ZZZUNIQ")
    assert sec == "77777"
    assert seg == "BSE_EQ"


def test_resolve_returns_none_when_unknown_everywhere(db):
    sec, seg = _resolve_security(db, date(2026, 4, 27), "NEVERSEEN")
    assert sec is None
    assert seg is None
