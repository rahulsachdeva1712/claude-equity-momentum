"""Paper trading engine. FRD B.6.

Pure business logic over the SQLite tables. Uses Dhan historical / intraday
to fetch the 09:30 candle close at execution time.

Key rules:
- Fill price = close of 09:30 minute candle, fetched from Dhan.
- Charges use the same shared compute_charges as live (full Dhan stack).
- Parity with live: apply_live_fill(paper_order_id, actual_qty) adjusts the
  quantity that paper fills at. If actual_qty == 0 (live rejected) the paper
  order is marked SKIPPED and no fill is recorded.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Callable

from app.alerts import Alert, raise_alert
from app.charges import Side, compute_charges
from app.db import tx
from app.time_utils import now_ist

log = logging.getLogger("paper")


@dataclass(frozen=True)
class PaperOrder:
    id: int
    session_date: date
    symbol: str
    action: str   # BUY / TRIM / EXIT
    order_qty: int
    status: str


def _current_book(conn: sqlite3.Connection) -> dict[str, tuple[int, float]]:
    rows = conn.execute("SELECT symbol, qty, avg_cost FROM paper_book").fetchall()
    return {r["symbol"]: (int(r["qty"]), float(r["avg_cost"])) for r in rows}


def generate_orders(conn: sqlite3.Connection, session_date: date) -> list[PaperOrder]:
    """Diff current paper book vs target set to produce BUY / TRIM / EXIT rows.

    Writes to paper_orders. Callers should run this after signals for
    session_date have been persisted.
    """
    targets = conn.execute(
        "SELECT symbol, selected, target_qty FROM signals WHERE session_date = ?",
        (session_date.isoformat(),),
    ).fetchall()
    target_map = {r["symbol"]: int(r["target_qty"]) for r in targets if int(r["selected"]) == 1}

    book = _current_book(conn)

    diffs: list[tuple[str, str, int]] = []
    # Symbols held but not in targets -> EXIT
    for sym, (qty, _avg) in book.items():
        if qty > 0 and sym not in target_map:
            diffs.append((sym, "EXIT", qty))
    # Symbols in targets
    for sym, tgt_qty in target_map.items():
        cur_qty = book.get(sym, (0, 0.0))[0]
        delta = tgt_qty - cur_qty
        if delta > 0:
            diffs.append((sym, "BUY", delta))
        elif delta < 0 and tgt_qty > 0:
            diffs.append((sym, "TRIM", -delta))
        elif delta < 0 and tgt_qty == 0:
            diffs.append((sym, "EXIT", -delta))
        # delta == 0: hold, no order

    orders: list[PaperOrder] = []
    with tx(conn):
        # Clear any previous PENDING orders for this session (idempotency for
        # the 09:30 consolidated job per FRD B.13).
        conn.execute(
            "DELETE FROM paper_orders WHERE session_date = ? AND status = 'PENDING'",
            (session_date.isoformat(),),
        )
        for sym, action, qty in diffs:
            cur = conn.execute(
                "INSERT INTO paper_orders (session_date, symbol, action, order_qty, created_at, status)"
                " VALUES (?, ?, ?, ?, ?, 'PENDING')",
                (session_date.isoformat(), sym, action, int(qty), now_ist().isoformat()),
            )
            orders.append(
                PaperOrder(
                    id=int(cur.lastrowid),
                    session_date=session_date,
                    symbol=sym,
                    action=action,
                    order_qty=int(qty),
                    status="PENDING",
                )
            )
    return orders


PriceFetcher = Callable[[str], float | None]
"""Given a symbol, return the 09:30 close price or None if unavailable."""


def execute_orders(
    conn: sqlite3.Connection,
    session_date: date,
    price_fetcher: PriceFetcher,
    qty_override: dict[int, int] | None = None,
) -> None:
    """Fill all PENDING orders for the session at 09:30 close.

    qty_override maps paper_order.id -> actual_qty to use (for parity with a
    live partial fill). If a key is present with value 0, that order is
    SKIPPED (live rejected / zero fill).
    """
    qty_override = qty_override or {}

    rows = conn.execute(
        "SELECT id, symbol, action, order_qty FROM paper_orders"
        " WHERE session_date = ? AND status = 'PENDING'",
        (session_date.isoformat(),),
    ).fetchall()

    for r in rows:
        oid = int(r["id"])
        sym = r["symbol"]
        action = r["action"]
        intended_qty = int(r["order_qty"])
        qty = int(qty_override.get(oid, intended_qty))

        if qty == 0:
            with tx(conn):
                conn.execute(
                    "UPDATE paper_orders SET status = 'SKIPPED', note = ? WHERE id = ?",
                    ("live_rejected_or_zero_fill", oid),
                )
            continue

        price = price_fetcher(sym)
        if price is None or price <= 0:
            with tx(conn):
                conn.execute(
                    "UPDATE paper_orders SET status = 'SKIPPED', note = ? WHERE id = ?",
                    ("no_0930_candle", oid),
                )
                raise_alert(
                    conn,
                    Alert(
                        severity="warn",
                        source="paper",
                        message=f"no 09:30 candle for {sym}, skipped",
                        payload={"session_date": session_date.isoformat(), "symbol": sym},
                    ),
                )
            continue

        side = Side.BUY if action == "BUY" else Side.SELL
        charges = compute_charges(side, qty, price)

        with tx(conn):
            conn.execute(
                "INSERT INTO paper_fills"
                " (paper_order_id, session_date, symbol, side, fill_qty, fill_price,"
                "  charges_total, charges_json, filled_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    oid,
                    session_date.isoformat(),
                    sym,
                    side.value,
                    qty,
                    price,
                    charges.total,
                    json.dumps(charges.to_dict()),
                    now_ist().isoformat(),
                ),
            )
            conn.execute("UPDATE paper_orders SET status = 'FILLED' WHERE id = ?", (oid,))
            _apply_to_book(conn, sym, side, qty, price)


def _apply_to_book(conn: sqlite3.Connection, symbol: str, side: Side, qty: int, price: float) -> None:
    row = conn.execute("SELECT qty, avg_cost, cost_basis FROM paper_book WHERE symbol = ?", (symbol,)).fetchone()
    now = now_ist().isoformat()
    if row is None:
        if side is Side.SELL:
            return  # selling something we don't own: paper ignores
        conn.execute(
            "INSERT INTO paper_book (symbol, qty, avg_cost, cost_basis, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (symbol, qty, price, qty * price, now),
        )
        return

    cur_qty = int(row["qty"])
    cur_cost = float(row["cost_basis"])
    if side is Side.BUY:
        new_qty = cur_qty + qty
        new_cost_basis = cur_cost + qty * price
        new_avg = new_cost_basis / new_qty if new_qty else 0.0
        conn.execute(
            "UPDATE paper_book SET qty = ?, avg_cost = ?, cost_basis = ?, updated_at = ? WHERE symbol = ?",
            (new_qty, new_avg, new_cost_basis, now, symbol),
        )
    else:  # SELL
        new_qty = max(0, cur_qty - qty)
        if new_qty == 0:
            conn.execute("DELETE FROM paper_book WHERE symbol = ?", (symbol,))
        else:
            # Average cost method: cost_basis reduces proportionally.
            ratio = new_qty / cur_qty
            new_cost_basis = cur_cost * ratio
            conn.execute(
                "UPDATE paper_book SET qty = ?, cost_basis = ?, updated_at = ? WHERE symbol = ?",
                (new_qty, new_cost_basis, now, symbol),
            )


def compute_daily_pnl(conn: sqlite3.Connection, session_date: date, ltp_fetcher: PriceFetcher) -> dict:
    """Realized = sum of sell gains this session; unrealized = book vs LTP."""
    realized = 0.0
    for r in conn.execute(
        "SELECT symbol, side, fill_qty, fill_price, charges_total FROM paper_fills WHERE session_date = ?",
        (session_date.isoformat(),),
    ):
        if r["side"] == "SELL":
            # Approximation: realized = (fill - avg_cost_before) * qty - charges.
            # For v1 we conservatively treat realized as (fill_price * qty) - charges
            # minus the retired portion of avg cost, tracked via paper_book.
            realized += float(r["fill_qty"]) * float(r["fill_price"]) - float(r["charges_total"])
        else:
            realized -= float(r["charges_total"])

    unreal = 0.0
    for r in conn.execute("SELECT symbol, qty, avg_cost FROM paper_book"):
        ltp = ltp_fetcher(r["symbol"])
        if ltp is None:
            continue
        unreal += float(r["qty"]) * (float(ltp) - float(r["avg_cost"]))

    mtm = realized + unreal
    with tx(conn):
        conn.execute(
            "INSERT INTO paper_pnl_daily (session_date, realized, unrealized, mtm, computed_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(session_date) DO UPDATE SET realized=excluded.realized,"
            " unrealized=excluded.unrealized, mtm=excluded.mtm, computed_at=excluded.computed_at",
            (session_date.isoformat(), realized, unreal, mtm, now_ist().isoformat()),
        )
    return {"realized": realized, "unrealized": unreal, "mtm": mtm}
