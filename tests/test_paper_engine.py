"""Tests for the paper engine. FRD B.6."""
from __future__ import annotations

from datetime import date

import pytest

from app.db import connect, init_db, tx
from app.paper.engine import (
    compute_daily_pnl,
    execute_orders,
    generate_orders,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    return connect()


def _seed_signals(conn, session_date: date, rows: list[tuple[str, int, int]]):
    """rows: (symbol, selected, target_qty)"""
    with tx(conn):
        for sym, sel, qty in rows:
            conn.execute(
                "INSERT INTO signals (session_date, symbol, selected, target_qty, rank_by_126d, target_weight, reference_price)"
                " VALUES (?, ?, ?, ?, 1, 0.2, 100.0)",
                (session_date.isoformat(), sym, sel, qty),
            )


def test_generate_orders_creates_buy_on_empty_book(db):
    d = date(2026, 4, 22)
    _seed_signals(db, d, [("CDG", 1, 140), ("CUPID", 1, 549)])
    orders = generate_orders(db, d)
    actions = sorted((o.symbol, o.action, o.order_qty) for o in orders)
    assert actions == [("CDG", "BUY", 140), ("CUPID", "BUY", 549)]


def test_generate_orders_creates_exit_when_held_but_not_targeted(db):
    d = date(2026, 4, 22)
    with tx(db):
        db.execute(
            "INSERT INTO paper_book (symbol, qty, avg_cost, cost_basis, updated_at) VALUES ('OLD', 100, 50.0, 5000.0, '2026-04-21')"
        )
    _seed_signals(db, d, [("NEW", 1, 10)])
    orders = generate_orders(db, d)
    syms = {o.symbol: (o.action, o.order_qty) for o in orders}
    assert syms["OLD"] == ("EXIT", 100)
    assert syms["NEW"] == ("BUY", 10)


def test_generate_orders_trims_when_target_lower(db):
    d = date(2026, 4, 22)
    with tx(db):
        db.execute(
            "INSERT INTO paper_book (symbol, qty, avg_cost, cost_basis, updated_at) VALUES ('HOLD', 200, 50.0, 10000.0, '2026-04-21')"
        )
    _seed_signals(db, d, [("HOLD", 1, 150)])
    orders = generate_orders(db, d)
    assert len(orders) == 1
    assert orders[0].action == "TRIM"
    assert orders[0].order_qty == 50


def test_execute_buy_updates_book_and_applies_charges(db):
    d = date(2026, 4, 22)
    _seed_signals(db, d, [("CDG", 1, 100)])
    generate_orders(db, d)

    execute_orders(db, d, price_fetcher=lambda s: 143.55)

    row = db.execute("SELECT qty, avg_cost FROM paper_book WHERE symbol = 'CDG'").fetchone()
    assert row["qty"] == 100
    assert row["avg_cost"] == pytest.approx(143.55)

    fill = db.execute("SELECT fill_qty, fill_price, charges_total FROM paper_fills WHERE symbol = 'CDG'").fetchone()
    assert fill["fill_qty"] == 100
    assert fill["fill_price"] == pytest.approx(143.55)
    assert fill["charges_total"] > 0


def test_parity_rule_qty_override_to_partial(db):
    """Live fills only 60 of 100 -> paper fills 60 too."""
    d = date(2026, 4, 22)
    _seed_signals(db, d, [("CDG", 1, 100)])
    orders = generate_orders(db, d)
    oid = orders[0].id

    execute_orders(db, d, price_fetcher=lambda s: 143.55, qty_override={oid: 60})

    row = db.execute("SELECT qty FROM paper_book WHERE symbol = 'CDG'").fetchone()
    assert row["qty"] == 60


def test_parity_rule_qty_override_zero_is_skipped(db):
    """Live rejected -> paper SKIPPED, no fill, no book change."""
    d = date(2026, 4, 22)
    _seed_signals(db, d, [("CDG", 1, 100)])
    orders = generate_orders(db, d)
    oid = orders[0].id

    execute_orders(db, d, price_fetcher=lambda s: 143.55, qty_override={oid: 0})

    row = db.execute("SELECT qty FROM paper_book WHERE symbol = 'CDG'").fetchone()
    assert row is None
    status = db.execute("SELECT status FROM paper_orders WHERE id = ?", (oid,)).fetchone()["status"]
    assert status == "SKIPPED"


def test_no_0930_candle_skips_and_alerts(db):
    d = date(2026, 4, 22)
    _seed_signals(db, d, [("HALT", 1, 10)])
    generate_orders(db, d)

    execute_orders(db, d, price_fetcher=lambda s: None)

    status = db.execute("SELECT status FROM paper_orders").fetchone()["status"]
    assert status == "SKIPPED"
    alert = db.execute("SELECT severity, message FROM alerts").fetchone()
    assert alert["severity"] == "warn"
    assert "HALT" in alert["message"]


def test_pnl_realized_plus_unrealized(db):
    d1 = date(2026, 4, 21)
    _seed_signals(db, d1, [("CDG", 1, 10)])
    generate_orders(db, d1)
    execute_orders(db, d1, price_fetcher=lambda s: 100.0)

    # compute PnL with LTP = 110 -> unrealized = 10 * (110-100) = 100 (minus charges)
    out = compute_daily_pnl(db, d1, ltp_fetcher=lambda s: 110.0)
    assert out["unrealized"] == pytest.approx(100.0)
    assert out["realized"] < 0  # only buy-side charges
    assert out["mtm"] < 100.0
