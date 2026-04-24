"""Read-model helpers for the web UI. Pure SQL -> Python dicts.
Keeps routes thin and templates clean.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

from app.dhan.client import jwt_expiry_epoch
from app.time_utils import IST


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


def top_bar_status(conn: sqlite3.Connection, token: str, worker_pid_alive: bool, live_enabled: bool) -> dict:
    token_state, token_label = _classify_token(token)
    market_status, market_age_s = _read_market_status(conn)
    return {
        "worker_alive": worker_pid_alive,
        "token_state": token_state,
        "token_label": token_label,
        "live_enabled": live_enabled,
        "market_status": market_status,
        "market_age_s": market_age_s,
        "unacked_alerts": len(alerts_unacked(conn)),
    }


# Stale threshold = 3 × polling cadence (30 s). If the worker missed three
# consecutive polls the pill falls back to 'unknown' — fail-safe on worker
# crash, token expiry, or Dhan outage.
MARKET_STATUS_STALE_SECONDS = 90


def _read_market_status(conn: sqlite3.Connection) -> tuple[str, int | None]:
    """Read the polled market-status row. Returns (label, age_seconds).

    - Missing row or stale row (> MARKET_STATUS_STALE_SECONDS) → ('unknown', age).
    - Fresh row → (uppercased Dhan value, age).

    Age is always returned so the template / CSS can show a staleness badge
    even while the label is 'unknown'.
    """
    row = conn.execute(
        "SELECT value, updated_at FROM settings WHERE key = 'market_status'"
    ).fetchone()
    if row is None:
        return "unknown", None
    try:
        updated = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return "unknown", None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=IST)
    age_s = int((datetime.now(IST) - updated.astimezone(IST)).total_seconds())
    if age_s > MARKET_STATUS_STALE_SECONDS:
        return "unknown", age_s
    return str(row["value"]).lower(), age_s


def _classify_token(token: str, now: datetime | None = None) -> tuple[str, str]:
    if not token:
        return "missing", "no token"
    exp_epoch = jwt_expiry_epoch(token)
    if exp_epoch is None:
        # Token is set but doesn't parse as JWT. Most common cause on Windows
        # is a UTF-8 BOM in .env from Notepad fusing into the variable name,
        # or the value being wrapped in quotes / extra whitespace.
        return "invalid", "token unparseable (check .env for BOM/quotes)"
    now_dt = now if now is not None else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    secs = exp_epoch - int(now_dt.timestamp())
    exp_ist = datetime.fromtimestamp(exp_epoch, tz=timezone.utc).astimezone(IST)
    hhmm = exp_ist.strftime("%H:%M")
    if secs <= 0:
        day_label = _day_phrase(exp_ist.date(), now_dt.astimezone(IST).date())
        return "expired", f"expired at {hhmm} IST {day_label} ({_format_ago(-secs)})"
    if secs <= 3600:
        return "expiring", f"expires in {secs // 60} min at {hhmm} IST"
    h = secs // 3600
    m = (secs % 3600) // 60
    return "valid", f"valid until {hhmm} IST ({h}h {m}m)"


def _format_ago(secs: int) -> str:
    """Compact relative-time suffix for past timestamps."""
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h {m}m ago"
    return f"{secs // 86400}d ago"


def _day_phrase(when: date, today: date) -> str:
    """Human-friendly day reference relative to today (both already in IST)."""
    delta = (today - when).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta == -1:
        return "tomorrow"
    if -7 < delta < 7:
        return when.strftime("%A")
    return when.isoformat()
