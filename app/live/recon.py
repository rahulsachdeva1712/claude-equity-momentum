"""Reconciliation with Dhan. FRD B.8.

Pulls Dhan positions book at 15s cadence during market hours, filters to
positions whose originating order carries our `emrb:` correlation tag,
and snapshots to `live_positions_snapshot`. Daily MTM rolls up from
tagged fills + LTP, cross-checked against Dhan's unrealizedProfit.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from app.alerts import Alert, raise_alert
from app.db import tx
from app.dhan.client import DhanClient
from app.dhan.models import Position
from app.time_utils import now_ist

log = logging.getLogger("recon")

DIVERGENCE_TOLERANCE = 0.005  # 0.5% of notional per FRD B.8


@dataclass(frozen=True)
class ReconSummary:
    tagged_symbols: int
    total_unrealized: float
    diverged: bool


def our_symbols(conn: sqlite3.Connection) -> set[str]:
    """Symbols we have ever traded under our tag. A position for any other
    symbol appearing in Dhan is ignored by the app.
    """
    rows = conn.execute("SELECT DISTINCT symbol FROM live_orders").fetchall()
    return {r["symbol"] for r in rows}


def filter_to_our_positions(positions: Iterable[Position], tagged: set[str]) -> list[Position]:
    return [p for p in positions if p.symbol in tagged]


async def snapshot_positions(conn: sqlite3.Connection, dhan: DhanClient) -> ReconSummary:
    raw = await dhan.positions()
    tagged = our_symbols(conn)
    ours = filter_to_our_positions(raw, tagged)
    now = now_ist().isoformat()

    unreal_ours = 0.0
    with tx(conn):
        for p in ours:
            conn.execute(
                "INSERT INTO live_positions_snapshot"
                " (taken_at, symbol, qty, avg_cost, ltp, unrealized, raw_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    p.symbol,
                    p.net_qty,
                    p.avg_cost,
                    p.ltp,
                    p.unrealized_pnl,
                    json.dumps(p.raw, default=str),
                ),
            )
            unreal_ours += p.unrealized_pnl

    # Divergence check: compute our view of unrealized and compare against the
    # sum Dhan returned for the same tagged symbols.
    internal_unreal = 0.0
    for p in ours:
        internal_unreal += p.net_qty * (p.ltp - p.avg_cost)

    notional = sum(abs(p.net_qty) * p.ltp for p in ours) or 1.0
    diff = abs(internal_unreal - unreal_ours)
    diverged = diff / notional > DIVERGENCE_TOLERANCE
    if diverged:
        raise_alert(
            conn,
            Alert(
                severity="warn",
                source="recon",
                message="internal vs Dhan PnL diverged beyond tolerance",
                payload={
                    "internal": internal_unreal,
                    "dhan": unreal_ours,
                    "diff": diff,
                    "notional": notional,
                    "tolerance": DIVERGENCE_TOLERANCE,
                },
            ),
        )

    return ReconSummary(
        tagged_symbols=len(ours),
        total_unrealized=unreal_ours,
        diverged=diverged,
    )


def compute_live_daily_pnl(conn: sqlite3.Connection, session_date: date) -> dict:
    """Aggregate realized (from today's fills) + unrealized (latest snapshot)."""
    realized = 0.0
    for r in conn.execute(
        "SELECT side, fill_qty, fill_price, charges_total FROM live_fills WHERE session_date = ?",
        (session_date.isoformat(),),
    ):
        if r["side"] == "SELL":
            realized += float(r["fill_qty"]) * float(r["fill_price"]) - float(r["charges_total"])
        else:
            realized -= float(r["charges_total"])

    unreal = 0.0
    latest = conn.execute(
        "SELECT MAX(taken_at) AS t FROM live_positions_snapshot"
    ).fetchone()["t"]
    if latest:
        for r in conn.execute(
            "SELECT unrealized FROM live_positions_snapshot WHERE taken_at = ?", (latest,)
        ):
            unreal += float(r["unrealized"])

    mtm = realized + unreal
    with tx(conn):
        conn.execute(
            "INSERT INTO live_pnl_daily (session_date, realized, unrealized, mtm, computed_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(session_date) DO UPDATE SET realized=excluded.realized,"
            " unrealized=excluded.unrealized, mtm=excluded.mtm, computed_at=excluded.computed_at",
            (session_date.isoformat(), realized, unreal, mtm, now_ist().isoformat()),
        )
    return {"realized": realized, "unrealized": unreal, "mtm": mtm}
