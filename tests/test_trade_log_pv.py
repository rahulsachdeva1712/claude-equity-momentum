"""Trade-log portfolio-value math: must include open-position MTM, not
just realized P&L. Regression test for the 2026-04-26 fix where the PV
tile was sticking at the seed for any buy-and-hold period.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.db import connect, init_db, tx
from app.web.views import day_grouped_trade_log


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    return connect()


def _seed_buy(conn, sd: str, symbol: str, qty: int, price: float):
    """Drop a single BUY fill + matching paper_book row for the day."""
    with tx(conn):
        conn.execute(
            "INSERT INTO paper_orders (session_date, symbol, action, order_qty, status, created_at)"
            " VALUES (?, ?, 'BUY', ?, 'FILLED', ?)",
            (sd, symbol, qty, f"{sd}T09:30:00+05:30"),
        )
        oid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO paper_fills"
            " (paper_order_id, session_date, symbol, side, fill_qty, fill_price,"
            "  charges_total, charges_json, filled_at)"
            " VALUES (?, ?, ?, 'BUY', ?, ?, 0.0, ?, ?)",
            (
                oid,
                sd,
                symbol,
                qty,
                price,
                '{"total": 0.0, "brokerage": 0.0}',
                f"{sd}T09:30:00+05:30",
            ),
        )
        conn.execute(
            "INSERT INTO paper_book (symbol, qty, avg_cost, cost_basis, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (symbol, qty, price, qty * price, f"{sd}T09:30:00+05:30"),
        )


def _seed_pnl_daily(conn, sd: str, realized: float, unrealized: float):
    with tx(conn):
        conn.execute(
            "INSERT INTO paper_pnl_daily (session_date, realized, unrealized, mtm, computed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (sd, realized, unrealized, realized + unrealized, f"{sd}T15:30:00+05:30"),
        )


def test_portfolio_value_includes_unrealized_on_buy_only_day(db):
    """Buy-only day, market moves up. PV must reflect the open MTM, not
    just the seed."""
    _seed_buy(db, "2026-04-24", "ADANIPOWER", 100, 200.0)  # 20,000 cost basis
    _seed_pnl_daily(db, "2026-04-24", realized=0.0, unrealized=500.0)

    groups = day_grouped_trade_log(db)
    assert len(groups) == 1
    g = groups[0]
    # seed (1L) + realized (0) + unrealized (500) = 1,00,500
    assert g["portfolio_value"] == pytest.approx(100_500.0)


def test_portfolio_value_zero_unrealized_when_pnl_row_missing(db):
    """If paper_pnl_daily has no row for that session, fall back to seed
    + realized cost-basis P&L only. (Defensive — should not normally
    happen since the execution job writes the row, but the trade log
    must not blow up.)"""
    _seed_buy(db, "2026-04-24", "ADANIPOWER", 100, 200.0)
    # no paper_pnl_daily row written

    groups = day_grouped_trade_log(db)
    g = groups[0]
    assert g["portfolio_value"] == pytest.approx(100_000.0)


def test_portfolio_value_compounds_realized_and_unrealized(db):
    """Day 1: BUY only (zero realized, +500 unrealized). Day 2: zero
    realized again, +1200 unrealized. Day 2 PV should reflect Day 2's
    open mark, not Day 1's; cumulative realized still propagates."""
    _seed_buy(db, "2026-04-23", "A", 100, 200.0)
    _seed_pnl_daily(db, "2026-04-23", realized=0.0, unrealized=500.0)

    _seed_buy(db, "2026-04-24", "B", 50, 100.0)
    _seed_pnl_daily(db, "2026-04-24", realized=0.0, unrealized=1200.0)

    groups = day_grouped_trade_log(db)
    by_date = {g["session_date"]: g for g in groups}
    # Day 1 PV uses Day 1's unrealized.
    assert by_date["2026-04-23"]["portfolio_value"] == pytest.approx(100_500.0)
    # Day 2 PV: realized still 0, today's unrealized 1200.
    assert by_date["2026-04-24"]["portfolio_value"] == pytest.approx(101_200.0)
