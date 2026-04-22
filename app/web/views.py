"""Read-model helpers for the web UI. Pure SQL -> Python dicts.
Keeps routes thin and templates clean.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from app.dhan.client import jwt_seconds_to_expiry


def _summary(conn: sqlite3.Connection, prefix: str) -> dict:
    """Prefix is 'paper' or 'live'. Returns headline tiles."""
    fills = conn.execute(
        f"SELECT side, fill_qty, fill_price, charges_total FROM {prefix}_fills"
    ).fetchall()
    realized_total = 0.0
    for r in fills:
        if r["side"] == "SELL":
            realized_total += r["fill_qty"] * r["fill_price"] - r["charges_total"]
        else:
            realized_total -= r["charges_total"]

    latest = conn.execute(f"SELECT realized, unrealized, mtm FROM {prefix}_pnl_daily ORDER BY session_date DESC LIMIT 1").fetchone()
    today = {"realized": 0.0, "unrealized": 0.0, "mtm": 0.0}
    if latest:
        today = {"realized": float(latest["realized"]), "unrealized": float(latest["unrealized"]), "mtm": float(latest["mtm"])}

    if prefix == "paper":
        book = conn.execute("SELECT COUNT(*) AS c FROM paper_book").fetchone()["c"]
    else:
        book = conn.execute(
            "SELECT COUNT(DISTINCT symbol) AS c FROM live_positions_snapshot"
            " WHERE taken_at = (SELECT MAX(taken_at) FROM live_positions_snapshot)"
        ).fetchone()["c"] or 0

    closed = conn.execute(f"SELECT COUNT(*) AS c FROM {prefix}_fills").fetchone()["c"]
    return {
        "today_mtm": today["mtm"],
        "today_realized": today["realized"],
        "today_unrealized": today["unrealized"],
        "cumulative": realized_total + today["unrealized"],
        "open_positions": int(book),
        "closed_fills": int(closed),
    }


def paper_summary(conn: sqlite3.Connection) -> dict:
    return _summary(conn, "paper")


def live_summary(conn: sqlite3.Connection) -> dict:
    return _summary(conn, "live")


def signals_for(conn: sqlite3.Connection, session_date: date) -> list[dict]:
    rows = conn.execute(
        "SELECT symbol, rank_by_126d, target_qty, target_weight, reference_price, selected"
        " FROM signals WHERE session_date = ? ORDER BY selected DESC, rank_by_126d",
        (session_date.isoformat(),),
    ).fetchall()
    return [dict(r) for r in rows]


def paper_book_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT symbol, qty, avg_cost, cost_basis, updated_at FROM paper_book ORDER BY symbol"
    ).fetchall()
    return [dict(r) for r in rows]


def live_positions(conn: sqlite3.Connection) -> list[dict]:
    latest = conn.execute("SELECT MAX(taken_at) AS t FROM live_positions_snapshot").fetchone()["t"]
    if not latest:
        return []
    rows = conn.execute(
        "SELECT symbol, qty, avg_cost, ltp, unrealized FROM live_positions_snapshot WHERE taken_at = ? ORDER BY symbol",
        (latest,),
    ).fetchall()
    return [dict(r) for r in rows]


def pnl_timeseries(conn: sqlite3.Connection, prefix: str, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        f"SELECT session_date, realized, unrealized, mtm FROM {prefix}_pnl_daily"
        " ORDER BY session_date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()
    return out


def recent_fills(conn: sqlite3.Connection, prefix: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        f"SELECT session_date, symbol, side, fill_qty, fill_price, charges_total, charges_json, filled_at"
        f" FROM {prefix}_fills ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            ch = json.loads(d.pop("charges_json"))
            d["non_broker_charges"] = round(ch["total"] - ch["brokerage"], 4)
        except Exception:  # noqa: BLE001
            d["non_broker_charges"] = None
        out.append(d)
    return out


def alerts_unacked(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT id, severity, source, message, created_at FROM alerts"
        " WHERE acknowledged_at IS NULL ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def top_bar_status(conn: sqlite3.Connection, token: str, worker_pid_alive: bool, live_enabled: bool, market_status: str | None) -> dict:
    secs = jwt_seconds_to_expiry(token) if token else None
    if secs is None:
        token_state = "missing"
        token_label = "no token"
    elif secs <= 0:
        token_state = "expired"
        token_label = "expired"
    elif secs <= 3600:
        token_state = "expiring"
        token_label = f"expires in {secs // 60} min"
    else:
        token_state = "valid"
        h = secs // 3600
        m = (secs % 3600) // 60
        token_label = f"valid, expires in {h}h {m}m"

    return {
        "worker_alive": worker_pid_alive,
        "token_state": token_state,
        "token_label": token_label,
        "live_enabled": live_enabled,
        "market_status": market_status or "unknown",
        "unacked_alerts": len(alerts_unacked(conn)),
    }
