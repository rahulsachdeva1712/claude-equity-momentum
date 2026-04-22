"""Tests for live engine + reconciliation. FRD B.7, B.8."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from app.db import connect, init_db, tx
from app.dhan.errors import DhanRejected, DhanUnavailable
from app.dhan.models import OrderStatus, Position
from app.live.engine import (
    is_live_enabled,
    make_correlation_id,
    place_orders,
    set_live_enabled,
)
from app.live.recon import (
    DIVERGENCE_TOLERANCE,
    compute_live_daily_pnl,
    filter_to_our_positions,
    our_symbols,
    snapshot_positions,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    return connect()


class FakeDhan:
    def __init__(self, responses: dict[str, Any] | None = None):
        self._responses = responses or {}
        self._order_statuses: dict[str, list[OrderStatus]] = {}
        self.placed: list = []
        self._positions_rows: list[Position] = []

    def queue_order_status(self, order_id: str, statuses: list[OrderStatus]) -> None:
        self._order_statuses[order_id] = list(statuses)

    def set_positions(self, rows: list[Position]) -> None:
        self._positions_rows = rows

    async def place_order(self, req):
        self.placed.append(req)
        r = self._responses.get(req.security_id)
        if isinstance(r, Exception):
            raise r
        return r or "ORD1"

    async def order_status(self, order_id: str) -> OrderStatus:
        q = self._order_statuses.get(order_id, [])
        if q:
            return q.pop(0)
        return OrderStatus(
            dhan_order_id=order_id,
            status="TRADED",
            filled_qty=10,
            ordered_qty=10,
            average_price=100.0,
            reject_reason=None,
            correlation_id=None,
            raw={},
        )

    async def positions(self) -> list[Position]:
        return list(self._positions_rows)


def test_correlation_id_format():
    c = make_correlation_id(date(2026, 4, 22), "CDG", "BUY")
    assert c.startswith("emrb:2026-04-22:CDG:BUY:")


def test_live_enabled_toggle(db):
    assert is_live_enabled(db) is False
    set_live_enabled(db, True)
    assert is_live_enabled(db) is True
    set_live_enabled(db, False)
    assert is_live_enabled(db) is False


@pytest.mark.asyncio
async def test_place_orders_records_fills_and_override(db):
    set_live_enabled(db, True)
    with tx(db):
        c = db.execute(
            "INSERT INTO paper_orders (session_date, symbol, action, order_qty, created_at, status)"
            " VALUES ('2026-04-22', 'CDG', 'BUY', 10, '2026-04-22T09:30', 'PENDING')"
        )
        paper_id = int(c.lastrowid)

    fake = FakeDhan(responses={"1": "ORD1"})
    fake.queue_order_status(
        "ORD1",
        [
            OrderStatus("ORD1", "OPEN", 0, 10, 0.0, None, None, {}),
            OrderStatus("ORD1", "TRADED", 10, 10, 143.55, None, None, {}),
        ],
    )

    overrides = await place_orders(
        db,
        fake,  # type: ignore[arg-type]
        date(2026, 4, 22),
        paper_orders=[(paper_id, "CDG", "BUY", 10, "1", "BSE_EQ")],
    )

    assert overrides[paper_id] == 10
    fill = db.execute("SELECT fill_qty, fill_price FROM live_fills").fetchone()
    assert fill["fill_qty"] == 10
    assert fill["fill_price"] == pytest.approx(143.55)
    assert len(fake.placed) == 1
    order = db.execute("SELECT status, correlation_id FROM live_orders").fetchone()
    assert order["status"] == "TRADED"
    assert order["correlation_id"].startswith("emrb:")


@pytest.mark.asyncio
async def test_reject_results_in_zero_override(db):
    set_live_enabled(db, True)
    with tx(db):
        c = db.execute(
            "INSERT INTO paper_orders (session_date, symbol, action, order_qty, created_at, status)"
            " VALUES ('2026-04-22', 'BADSYM', 'BUY', 10, '2026-04-22T09:30', 'PENDING')"
        )
        paper_id = int(c.lastrowid)

    fake = FakeDhan(responses={"99": DhanRejected("no margin")})
    overrides = await place_orders(
        db, fake, date(2026, 4, 22),  # type: ignore[arg-type]
        paper_orders=[(paper_id, "BADSYM", "BUY", 10, "99", "BSE_EQ")],
    )

    assert overrides[paper_id] == 0
    row = db.execute("SELECT status, reject_reason FROM live_orders").fetchone()
    assert row["status"] == "REJECTED"
    assert "margin" in (row["reject_reason"] or "")
    # and no live_fills written
    n = db.execute("SELECT COUNT(*) AS c FROM live_fills").fetchone()["c"]
    assert n == 0


@pytest.mark.asyncio
async def test_kill_switch_halts_placement(db):
    set_live_enabled(db, True)
    with tx(db):
        a = db.execute(
            "INSERT INTO paper_orders (session_date, symbol, action, order_qty, created_at, status)"
            " VALUES ('2026-04-22', 'A', 'BUY', 10, '2026-04-22T09:30', 'PENDING')"
        ).lastrowid
        b = db.execute(
            "INSERT INTO paper_orders (session_date, symbol, action, order_qty, created_at, status)"
            " VALUES ('2026-04-22', 'B', 'BUY', 10, '2026-04-22T09:30', 'PENDING')"
        ).lastrowid

    class KillFake(FakeDhan):
        async def place_order(self, req):
            # Flip kill switch after the first order is placed.
            set_live_enabled(db, False)
            return await super().place_order(req)

    fake = KillFake()
    fake.queue_order_status("ORD1", [OrderStatus("ORD1", "TRADED", 10, 10, 100.0, None, None, {})])

    overrides = await place_orders(
        db, fake, date(2026, 4, 22),  # type: ignore[arg-type]
        paper_orders=[(int(a), "A", "BUY", 10, "1", "BSE_EQ"), (int(b), "B", "BUY", 10, "2", "BSE_EQ")],
    )

    # first placed and fully traded, second never placed -> not in overrides
    assert int(a) in overrides
    assert int(b) not in overrides
    n_orders = db.execute("SELECT COUNT(*) AS c FROM live_orders").fetchone()["c"]
    assert n_orders == 1


def _pos(symbol: str, qty: int, avg: float, ltp: float, unreal: float) -> Position:
    return Position(
        symbol=symbol, security_id="?", exchange_segment="BSE_EQ",
        net_qty=qty, avg_cost=avg, ltp=ltp, unrealized_pnl=unreal, realized_pnl=0.0, raw={},
    )


def test_filter_to_our_positions_ignores_manual(db):
    # Seed one tagged symbol; simulate Dhan returning both tagged and manual.
    with tx(db):
        db.execute(
            "INSERT INTO live_orders (session_date, symbol, action, order_qty, correlation_id, status, placed_at)"
            " VALUES ('2026-04-22', 'CDG', 'BUY', 10, 'emrb:x', 'TRADED', '2026-04-22T09:30')"
        )
    ours_set = our_symbols(db)
    rows = [_pos("CDG", 10, 100.0, 110.0, 100.0), _pos("MANUAL", 5, 50.0, 55.0, 25.0)]
    filtered = filter_to_our_positions(rows, ours_set)
    assert [p.symbol for p in filtered] == ["CDG"]


@pytest.mark.asyncio
async def test_snapshot_positions_writes_only_ours(db):
    with tx(db):
        db.execute(
            "INSERT INTO live_orders (session_date, symbol, action, order_qty, correlation_id, status, placed_at)"
            " VALUES ('2026-04-22', 'CDG', 'BUY', 10, 'emrb:x', 'TRADED', '2026-04-22T09:30')"
        )

    fake = FakeDhan()
    fake.set_positions([_pos("CDG", 10, 100.0, 110.0, 100.0), _pos("MANUAL", 5, 50.0, 55.0, 25.0)])

    summary = await snapshot_positions(db, fake)  # type: ignore[arg-type]
    assert summary.tagged_symbols == 1
    rows = db.execute("SELECT symbol FROM live_positions_snapshot").fetchall()
    assert [r["symbol"] for r in rows] == ["CDG"]


@pytest.mark.asyncio
async def test_snapshot_alerts_on_divergence(db):
    with tx(db):
        db.execute(
            "INSERT INTO live_orders (session_date, symbol, action, order_qty, correlation_id, status, placed_at)"
            " VALUES ('2026-04-22', 'CDG', 'BUY', 10, 'emrb:x', 'TRADED', '2026-04-22T09:30')"
        )

    fake = FakeDhan()
    # Dhan says unrealized = 100, but (ltp-avg)*qty = 10*(110-100) = 100, no divergence.
    # Force divergence by setting Dhan's reported unreal wildly off.
    fake.set_positions([_pos("CDG", 10, 100.0, 110.0, unreal=500.0)])  # mismatch

    summary = await snapshot_positions(db, fake)  # type: ignore[arg-type]
    assert summary.diverged is True
    a = db.execute("SELECT severity, source FROM alerts").fetchone()
    assert a["severity"] == "warn"
    assert a["source"] == "recon"


def test_compute_live_daily_pnl_rolls_up_fills_and_snapshot(db):
    d = date(2026, 4, 22)
    with tx(db):
        # An order + fill
        o = db.execute(
            "INSERT INTO live_orders (session_date, symbol, action, order_qty, correlation_id, status, placed_at, terminal_at)"
            " VALUES (?, 'CDG', 'BUY', 10, 'emrb:x', 'TRADED', ?, ?)",
            (d.isoformat(), "2026-04-22T09:30", "2026-04-22T09:31"),
        ).lastrowid
        db.execute(
            "INSERT INTO live_fills (live_order_id, session_date, symbol, side, fill_qty, fill_price, charges_total, charges_json, filled_at)"
            " VALUES (?, ?, 'CDG', 'BUY', 10, 100.0, 5.0, '{}', ?)",
            (o, d.isoformat(), "2026-04-22T09:31"),
        )
        db.execute(
            "INSERT INTO live_positions_snapshot (taken_at, symbol, qty, avg_cost, ltp, unrealized, raw_json)"
            " VALUES ('2026-04-22T10:00', 'CDG', 10, 100.0, 110.0, 100.0, '{}')"
        )

    out = compute_live_daily_pnl(db, d)
    assert out["realized"] == pytest.approx(-5.0)  # buy-side charges only
    assert out["unrealized"] == pytest.approx(100.0)
    assert out["mtm"] == pytest.approx(95.0)
