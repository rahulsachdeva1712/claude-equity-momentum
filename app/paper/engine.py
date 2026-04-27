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

    # SELLs (TRIM/EXIT) first so realised cash funds the BUYs in this same
    # session — otherwise a 100% rebalance from "fully invested in 3 names"
    # to "1 new name" would buy on top of held positions and double-leverage
    # the book. ORDER BY action puts BUY last (B < E < T alphabetically, but
    # "BUY" sorts before "EXIT"/"TRIM" — invert with CASE).
    rows = conn.execute(
        "SELECT id, symbol, action, order_qty FROM paper_orders"
        " WHERE session_date = ? AND status = 'PENDING'"
        " ORDER BY CASE action WHEN 'EXIT' THEN 0 WHEN 'TRIM' THEN 1 ELSE 2 END, id",
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

        # Cash gate: BUYs are funded by the seed plus any SELLs already
        # executed in this session. Because the row order is SELL-first
        # (see ORDER BY above), `paper_cash(conn)` here reflects post-SELL
        # cash. If there isn't enough to fill `qty` at `price` plus
        # estimated charges, scale the order down to what fits — never
        # buy on credit. A skip is preferred only when the resulting qty
        # would be zero.
        if side is Side.BUY:
            cash_now = paper_cash(conn)
            est_charges = compute_charges(side, qty, price).total
            need = qty * price + est_charges
            if need > cash_now + 1e-6:
                # Scale qty down. Iteratively recompute charges since they
                # depend on qty; one pass is enough for the closed-form
                # charge schedule used in compute_charges.
                affordable_qty = max(0, int((cash_now * (1.0 - 0.005)) // price))
                # Re-check with actual charges at the affordable qty.
                if affordable_qty > 0:
                    actual = compute_charges(side, affordable_qty, price).total
                    while affordable_qty > 0 and (affordable_qty * price + actual) > cash_now:
                        affordable_qty -= 1
                        actual = compute_charges(side, affordable_qty, price).total if affordable_qty else 0.0
                if affordable_qty <= 0:
                    with tx(conn):
                        conn.execute(
                            "UPDATE paper_orders SET status = 'SKIPPED', note = ? WHERE id = ?",
                            ("insufficient_cash", oid),
                        )
                        raise_alert(
                            conn,
                            Alert(
                                severity="warn",
                                source="paper",
                                message=(
                                    f"BUY {sym}: insufficient cash "
                                    f"(have {cash_now:.2f}, need {need:.2f}); skipped"
                                ),
                                payload={"session_date": session_date.isoformat(), "symbol": sym},
                            ),
                        )
                    continue
                log.warning(
                    "BUY %s: scaled qty %d -> %d to fit cash %.2f",
                    sym, qty, affordable_qty, cash_now,
                )
                qty = affordable_qty

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


def _cost_basis_realized_per_session(conn: sqlite3.Connection) -> dict[str, float]:
    """Replay all paper_fills in chronological order through a running
    average-cost book and return ``{session_date: realized_pnl}``.

    Realised on a SELL = ``(fill_price - avg_cost_at_time) * fill_qty`` —
    the same average-cost method ``_apply_to_book`` uses on the live
    ``paper_book``. Used both by ``compute_daily_pnl`` (writing
    ``paper_pnl_daily.realized``) and by the headline KPI / trade-log
    views, so the two never diverge. Cheap enough to recompute every
    call: ``paper_fills`` is small.

    Charges are excluded — they surface separately at the foot of the
    Trade Log so the headline numbers track gross gain/loss.
    """
    state: dict[str, dict] = {}  # symbol -> {qty, cost_basis}
    out: dict[str, float] = {}
    for r in conn.execute(
        "SELECT session_date, symbol, side, fill_qty, fill_price"
        " FROM paper_fills ORDER BY filled_at, id"
    ):
        sd = r["session_date"]
        sym = r["symbol"]
        st = state.setdefault(sym, {"qty": 0, "cost_basis": 0.0})
        qty = int(r["fill_qty"])
        price = float(r["fill_price"])
        if r["side"] == "BUY":
            st["qty"] += qty
            st["cost_basis"] += qty * price
        else:  # SELL
            if st["qty"] <= 0:
                continue  # selling something we don't own; ignore
            avg = st["cost_basis"] / st["qty"]
            sell_qty = min(qty, st["qty"])
            pnl = (price - avg) * sell_qty
            out[sd] = out.get(sd, 0.0) + pnl
            st["cost_basis"] -= avg * sell_qty
            st["qty"] -= sell_qty
            if st["qty"] == 0:
                st["cost_basis"] = 0.0
    return out


PAPER_INITIAL_CAPITAL_KEY = "paper_initial_capital"
PAPER_INITIAL_CAPITAL_DEFAULT = 100_000.0


def paper_initial_capital(conn: sqlite3.Connection) -> float:
    """Seed cash for the paper book. Defaults to ₹1L; overridable via the
    ``settings.paper_initial_capital`` row so a future "fund my paper account"
    UI can change it without code edits."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (PAPER_INITIAL_CAPITAL_KEY,)
    ).fetchone()
    if row:
        try:
            return float(row["value"])
        except (TypeError, ValueError):
            pass
    return PAPER_INITIAL_CAPITAL_DEFAULT


def paper_cash(conn: sqlite3.Connection) -> float:
    """Cash available to the paper book = seed - net BUY/SELL notional - charges.

    Recomputed from ``paper_fills`` every call so we never drift out of sync
    with positions; the table is small and indexed. Charges are tracked even
    though brokerage is zero on Dhan CNC delivery — non-broker statutory
    charges (STT, exchange txn, SEBI, stamp, GST) still leak real cash.
    """
    seed = paper_initial_capital(conn)
    cash = seed
    for f in conn.execute(
        "SELECT side, fill_qty, fill_price, charges_total FROM paper_fills"
    ):
        notional = float(f["fill_qty"]) * float(f["fill_price"])
        charges = float(f["charges_total"] or 0.0)
        if f["side"] == "BUY":
            cash -= notional + charges
        else:  # SELL
            cash += notional - charges
    return cash


def paper_portfolio_value(conn: sqlite3.Connection) -> float:
    """Current portfolio value = cash + sum(qty × marked_price) for open
    positions. Marked price uses the same fallback chain as
    ``web.views.book_rich``: ``live_ltp`` → last BUY fill price →
    ``avg_cost`` (so a fresh entry contributes notional == cost basis,
    not zero).

    This is the number the rebalance should size against — never a
    hardcoded notional. Otherwise every rebalance assumes a fresh seed
    and either over-deploys (the 2026-04-26 leverage bug) or chronically
    under-deploys a winning portfolio.
    """
    cash = paper_cash(conn)
    mv = 0.0
    fetcher = _build_fallback_ltp_fetcher(conn)
    for r in conn.execute("SELECT symbol, qty, avg_cost FROM paper_book"):
        qty = int(r["qty"])
        if qty <= 0:
            continue
        price = fetcher(r["symbol"])
        if price is None:
            price = float(r["avg_cost"])
        mv += qty * float(price)
    return cash + mv


def _build_fallback_ltp_fetcher(conn: sqlite3.Connection) -> PriceFetcher:
    """Default LTP fetcher used when no explicit one is supplied.

    Mirrors the priority chain in ``web.views.book_rich`` so the headline
    KPI tile and the Current Paper Book agree on marked prices:

        live_ltp (worker-polled minute candle) → last BUY fill price → unknown

    "unknown" means the per-symbol contribution to unrealized is zero (the
    caller's ``continue``). Last-fill fallback yields zero for fresh entries
    too (LTP == avg_cost on the day of fill), but keeps positions priced
    overnight / over weekends when ``live_ltp`` is empty so the row doesn't
    silently re-zero on every off-hours refresh.
    """
    ltp_map: dict[str, float] = {}
    try:
        for r in conn.execute("SELECT symbol, ltp FROM live_ltp"):
            ltp_map[r["symbol"]] = float(r["ltp"])
    except sqlite3.OperationalError:
        pass

    last_buy: dict[str, float] = {}
    for r in conn.execute(
        "SELECT symbol, fill_price FROM paper_fills WHERE side='BUY' ORDER BY filled_at"
    ):
        last_buy[r["symbol"]] = float(r["fill_price"])

    def fetch(symbol: str) -> float | None:
        if symbol in ltp_map:
            return ltp_map[symbol]
        if symbol in last_buy:
            return last_buy[symbol]
        return None

    return fetch


def compute_daily_pnl(
    conn: sqlite3.Connection,
    session_date: date,
    ltp_fetcher: PriceFetcher | None = None,
) -> dict:
    """Realized = sum of sell gains this session; unrealized = book vs LTP.

    Non-broker charges (STT, exch txn, SEBI, stamp, GST) are **not** deducted
    from realized P&L — they surface separately at the foot of the Trade Log
    so the headline numbers track gross gain/loss. Brokerage is zero for Dhan
    CNC delivery so no broker-fee leakage either.

    ``ltp_fetcher`` is optional. The 09:30 execution job passes its
    same-session 09:30 close fetcher; the periodic MTM refresh job (FRD B.5)
    passes nothing and lets us build the ``live_ltp``-based fallback chain
    so today's row stays in sync with the open paper book even outside the
    market window.
    """
    if ltp_fetcher is None:
        ltp_fetcher = _build_fallback_ltp_fetcher(conn)

    # Realized = cost-basis P&L on SELLs in this session, computed by
    # replaying the full fills history through a running average-cost book.
    # The previous v1 implementation summed SELL *notional* and was wrong by
    # the cost basis — fine when there were no SELLs (Champion B was buy-and-
    # hold for two days), broken the moment a rebalance fired an EXIT. Parity
    # with the trade-log replay in web.views.day_grouped_trade_log is now
    # exact (both use this helper).
    per_day_realized = _cost_basis_realized_per_session(conn)
    realized = float(per_day_realized.get(session_date.isoformat(), 0.0))

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
