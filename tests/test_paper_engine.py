"""Tests for the paper engine. FRD B.6."""
from __future__ import annotations

from datetime import date

import pytest

from app.db import connect, init_db, tx
from app.paper.engine import (
    compute_daily_pnl,
    execute_orders,
    generate_orders,
    paper_cash,
    paper_portfolio_value,
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

    # compute PnL with LTP = 110 -> unrealized = 10 * (110-100) = 100.
    # Non-broker charges are tracked separately (Trade Log footer) and do
    # NOT hit realized; BUY-only day → realized must be exactly zero.
    out = compute_daily_pnl(db, d1, ltp_fetcher=lambda s: 110.0)
    assert out["unrealized"] == pytest.approx(100.0)
    assert out["realized"] == pytest.approx(0.0)
    assert out["mtm"] == pytest.approx(100.0)


def test_pnl_uses_live_ltp_when_no_fetcher_passed(db):
    """The MTM refresh job calls compute_daily_pnl with no fetcher; the
    engine should fall through to live_ltp for the marked price."""
    d1 = date(2026, 4, 21)
    _seed_signals(db, d1, [("CDG", 1, 10)])
    generate_orders(db, d1)
    execute_orders(db, d1, price_fetcher=lambda s: 100.0)

    with tx(db):
        db.execute(
            "INSERT INTO live_ltp (symbol, ltp, fetched_at) VALUES (?, ?, ?)",
            ("CDG", 115.0, "2026-04-21T15:30:00+05:30"),
        )

    out = compute_daily_pnl(db, d1)  # no fetcher
    assert out["unrealized"] == pytest.approx(150.0)  # 10 * (115 - 100)
    assert out["realized"] == pytest.approx(0.0)
    assert out["mtm"] == pytest.approx(150.0)


def test_pnl_falls_back_to_last_buy_when_live_ltp_empty(db):
    """When live_ltp has no row for the symbol, the engine falls back to
    the last BUY fill price. That yields zero unrealized for fresh entries
    (LTP == avg_cost) but at least keeps the row from contradicting an
    existing book overnight when the LTP cache is empty."""
    d1 = date(2026, 4, 21)
    _seed_signals(db, d1, [("CDG", 1, 10)])
    generate_orders(db, d1)
    execute_orders(db, d1, price_fetcher=lambda s: 100.0)

    out = compute_daily_pnl(db, d1)  # no fetcher, no live_ltp row
    # Last BUY fill price is 100, avg_cost is 100 → unrealized = 0.
    assert out["unrealized"] == pytest.approx(0.0)
    assert out["realized"] == pytest.approx(0.0)


def test_summary_unrealized_matches_current_book_total(db):
    """KPI tile and Current Book table must read the same unrealized number.
    Before the fix, the tile read paper_pnl_daily.unrealized (a once-a-minute
    snapshot) while the table computed live from `live_ltp` — so a price
    move between refresh ticks made them diverge until the next refresh.
    Now both go through book_rich's live computation."""
    from app.web.views import paper_summary, book_rich

    d1 = date(2026, 4, 21)
    _seed_signals(db, d1, [("CDG", 1, 10)])
    generate_orders(db, d1)
    execute_orders(db, d1, price_fetcher=lambda s: 100.0)

    # Mark the position up via live_ltp.
    with tx(db):
        db.execute(
            "INSERT INTO live_ltp (symbol, ltp, fetched_at) VALUES (?, ?, ?)",
            ("CDG", 130.0, "2026-04-21T15:30:00+05:30"),
        )

    # Write a STALE paper_pnl_daily row simulating an out-of-date refresh
    # (e.g. live_ltp was 110 when the last `paper_mtm_refresh_job` ran).
    with tx(db):
        db.execute(
            "INSERT INTO paper_pnl_daily (session_date, realized, unrealized, mtm, computed_at)"
            " VALUES (?, 0.0, 100.0, 100.0, ?)"
            " ON CONFLICT(session_date) DO UPDATE SET realized=excluded.realized,"
            " unrealized=excluded.unrealized, mtm=excluded.mtm, computed_at=excluded.computed_at",
            (d1.isoformat(), "2026-04-21T15:29:00+05:30"),
        )

    s = paper_summary(db)
    book_total = sum(b["unrealized_pnl"] for b in book_rich(db, "paper"))
    # Live truth: 10 * (130 - 100) = 300, NOT the stale 100 in paper_pnl_daily.
    assert s["today_unrealized"] == pytest.approx(300.0)
    assert s["today_unrealized"] == pytest.approx(book_total)
    # today_mtm and cumulative also rebuild off live unrealized.
    assert s["today_mtm"] == pytest.approx(s["today_realized"] + s["today_unrealized"])


def test_summary_includes_portfolio_value_for_paper(db):
    """The headline KPI tile reads ``summary.portfolio_value`` directly
    from ``paper_portfolio_value(conn)``. Pin the wiring so a future
    refactor of either function keeps them in sync."""
    from app.web.views import paper_summary

    s = paper_summary(db)
    assert "portfolio_value" in s
    assert s["portfolio_value"] == pytest.approx(paper_portfolio_value(db))


def test_summary_omits_portfolio_value_for_live(db):
    """Live PV would have to come off the broker snapshot, not paper
    cash — keep the field paper-only so a `{% if … is defined %}`
    template guard keeps the live tab clean."""
    from app.web.views import live_summary

    s = live_summary(db)
    assert "portfolio_value" not in s


def test_paper_cash_starts_at_seed(db):
    """A fresh book has no fills, so cash equals the ₹1L seed exactly."""
    assert paper_cash(db) == pytest.approx(100_000.0)


def test_paper_cash_decreases_on_buy_increases_on_sell(db):
    """Cash math is symmetric and reflects charges on both sides."""
    d1 = date(2026, 4, 21)
    _seed_signals(db, d1, [("CDG", 1, 10)])
    generate_orders(db, d1)
    execute_orders(db, d1, price_fetcher=lambda s: 100.0)

    cash_after_buy = paper_cash(db)
    # 10 * 100 = 1000 spent (+ tiny non-broker charges).
    assert cash_after_buy < 99_000.0
    assert cash_after_buy > 98_900.0  # generous bound — charges are small

    # Now exit the position at 110 -> cash returns + a profit.
    d2 = date(2026, 4, 22)
    _seed_signals(db, d2, [])  # nothing selected -> EXIT diff
    generate_orders(db, d2)
    execute_orders(db, d2, price_fetcher=lambda s: 110.0)

    cash_after_sell = paper_cash(db)
    # Net P&L from 100 -> 110 on 10 shares = +100, minus charges both legs.
    assert cash_after_sell > 100_000.0  # we made money
    assert cash_after_sell < 100_100.0  # bounded by gross P&L


def test_paper_pv_equals_cash_plus_marked_book(db):
    """PV = cash + sum(qty × marked_price). Marked uses live_ltp when
    present, otherwise falls back to last BUY fill price (== avg_cost
    on a fresh entry, so unrealized = 0 and PV = seed - charges)."""
    d1 = date(2026, 4, 21)
    _seed_signals(db, d1, [("CDG", 1, 10)])
    generate_orders(db, d1)
    execute_orders(db, d1, price_fetcher=lambda s: 100.0)

    # Without live_ltp: marked = last fill = 100 = avg_cost. PV = cash + 1000.
    pv = paper_portfolio_value(db)
    assert pv == pytest.approx(paper_cash(db) + 1000.0)
    # Charges leak from PV (small), so PV slightly under seed.
    assert pv < 100_000.0
    assert pv > 99_900.0

    # Mark up the position via live_ltp -> PV jumps by the unrealized.
    with tx(db):
        db.execute(
            "INSERT INTO live_ltp (symbol, ltp, fetched_at) VALUES (?, ?, ?)",
            ("CDG", 130.0, "2026-04-21T15:30:00+05:30"),
        )
    pv2 = paper_portfolio_value(db)
    # PV gain = 10 * (130 - 100) = 300 above the no-mark value.
    assert pv2 == pytest.approx(pv + 300.0)


def test_buy_scaled_down_when_cash_insufficient(db):
    """If a BUY's notional exceeds available cash, the engine fills a
    smaller qty (rather than skipping outright) so the rebalance still
    deploys what it can."""
    d0 = date(2026, 4, 20)  # prior session — must use a DIFFERENT date so
    d1 = date(2026, 4, 21)  # generate_orders' "delete PENDING for d1" doesn't
    # cascade-delete the fake prior fill (FK ON DELETE CASCADE).

    with tx(db):
        db.execute(
            "INSERT INTO paper_book (symbol, qty, avg_cost, cost_basis, updated_at)"
            " VALUES ('OLD', 800, 100.0, 80000.0, '2026-04-20')"
        )
        db.execute(
            "INSERT INTO paper_orders (session_date, symbol, action, order_qty, status, created_at)"
            " VALUES (?, 'OLD', 'BUY', 800, 'FILLED', '2026-04-20')",
            (d0.isoformat(),),
        )
        db.execute(
            "INSERT INTO paper_fills"
            " (paper_order_id, session_date, symbol, side, fill_qty, fill_price,"
            "  charges_total, charges_json, filled_at)"
            " VALUES (1, ?, 'OLD', 'BUY', 800, 100.0, 0.0, '{}', '2026-04-20T09:30:00+05:30')",
            (d0.isoformat(),),
        )
    # Strategy wants to buy 1500 NEW @ 100 = 150k notional, but cash post-EXIT
    # of OLD will only be ~100k. Scaling should kick in.
    _seed_signals(db, d1, [("NEW", 1, 1500)])
    generate_orders(db, d1)

    # SELL OLD frees ~80k cleanly, then NEW must scale to fit.
    execute_orders(db, d1, price_fetcher=lambda s: 100.0)

    # NEW order should be FILLED with a scaled-down qty (< 1500), and the
    # cash-after must be near zero (book fully invested) but never negative.
    new_fill = db.execute(
        "SELECT fill_qty FROM paper_fills WHERE symbol = 'NEW' AND side = 'BUY'"
    ).fetchone()
    assert new_fill is not None, "BUY should have been scaled, not skipped"
    assert int(new_fill["fill_qty"]) < 1500
    assert int(new_fill["fill_qty"]) > 0
    assert paper_cash(db) >= 0.0


def test_buy_skipped_with_alert_when_no_affordable_qty(db):
    """If even one share doesn't fit the available cash, the BUY skips
    with an `insufficient_cash` note + warn alert — better than burning
    margin we don't have."""
    d0 = date(2026, 4, 20)
    d1 = date(2026, 4, 21)
    with tx(db):
        db.execute(
            "INSERT INTO paper_orders (session_date, symbol, action, order_qty, status, created_at)"
            " VALUES (?, 'PRIOR', 'BUY', 1, 'FILLED', '2026-04-20')",
            (d0.isoformat(),),
        )
        db.execute(
            "INSERT INTO paper_fills"
            " (paper_order_id, session_date, symbol, side, fill_qty, fill_price,"
            "  charges_total, charges_json, filled_at)"
            " VALUES (1, ?, 'PRIOR', 'BUY', 1, 100000.0, 0.0, '{}',"
            " '2026-04-20T09:30:00+05:30')",
            (d0.isoformat(),),
        )
        db.execute(
            "INSERT INTO paper_book (symbol, qty, avg_cost, cost_basis, updated_at)"
            " VALUES ('PRIOR', 1, 100000.0, 100000.0, '2026-04-20')"
        )
    assert paper_cash(db) == pytest.approx(0.0)

    _seed_signals(db, d1, [("NEW", 1, 5)])
    generate_orders(db, d1)
    execute_orders(db, d1, price_fetcher=lambda s: 1000.0)  # 5 * 1000 = 5000 needed, 0 available

    order = db.execute(
        "SELECT status, note FROM paper_orders WHERE symbol = 'NEW'"
    ).fetchone()
    assert order["status"] == "SKIPPED"
    assert order["note"] == "insufficient_cash"
    # No fill row created.
    assert db.execute(
        "SELECT COUNT(*) AS c FROM paper_fills WHERE symbol = 'NEW'"
    ).fetchone()["c"] == 0


def test_sells_execute_before_buys_in_same_session(db):
    """The order ORDER BY clause must put EXIT/TRIM ahead of BUY so a
    full rotation (sell 3 names, buy 1 new) doesn't run BUY on top of
    held positions and double-leverage. Regression test for the
    2026-04-26 leverage bug."""
    d0 = date(2026, 4, 20)
    d1 = date(2026, 4, 21)
    with tx(db):
        db.execute(
            "INSERT INTO paper_book (symbol, qty, avg_cost, cost_basis, updated_at)"
            " VALUES ('OLD', 1000, 100.0, 100000.0, '2026-04-20')"
        )
        db.execute(
            "INSERT INTO paper_orders (session_date, symbol, action, order_qty, status, created_at)"
            " VALUES (?, 'OLD', 'BUY', 1000, 'FILLED', '2026-04-20')",
            (d0.isoformat(),),
        )
        db.execute(
            "INSERT INTO paper_fills"
            " (paper_order_id, session_date, symbol, side, fill_qty, fill_price,"
            "  charges_total, charges_json, filled_at)"
            " VALUES (1, ?, 'OLD', 'BUY', 1000, 100.0, 0.0, '{}',"
            " '2026-04-20T09:30:00+05:30')",
            (d0.isoformat(),),
        )
    # Today: drop OLD, add NEW. Without SELL-first, NEW can't be funded.
    _seed_signals(db, d1, [("NEW", 1, 800)])  # 800 * 100 = 80k notional
    generate_orders(db, d1)
    execute_orders(db, d1, price_fetcher=lambda s: 100.0)

    # OLD must be exited (SELL filled), NEW must be filled — both, not just one.
    fills = {
        r["symbol"]: dict(r)
        for r in db.execute(
            "SELECT symbol, side, fill_qty FROM paper_fills WHERE session_date = ?",
            (d1.isoformat(),),
        ).fetchall()
    }
    assert fills["OLD"]["side"] == "SELL"
    assert fills["OLD"]["fill_qty"] == 1000
    assert fills["NEW"]["side"] == "BUY"
    assert fills["NEW"]["fill_qty"] == 800
    # paper_book: OLD gone, NEW added.
    book = {r["symbol"]: dict(r) for r in db.execute("SELECT * FROM paper_book")}
    assert "OLD" not in book
    assert book["NEW"]["qty"] == 800
