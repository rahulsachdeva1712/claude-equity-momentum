"""Read-model helpers for the web UI. Pure SQL -> Python dicts.
Keeps routes thin and templates clean.

Paper tab aims for parity with (and exceeds) a full rebalance-backtester
dashboard — KPIs, today-status row, dual P&L charts, perf-summary grid,
rich per-position book with live marks, and day-grouped trade log. The
richer view-model lives here so the Jinja template stays declarative.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timezone
from typing import Any

from app.dhan.client import jwt_expiry_epoch
from app.time_utils import IST, now_ist, session_date_for


# --------------------------------------------------------------------------
# Compatibility: used by Live tab + existing tests.
# --------------------------------------------------------------------------


def _summary(conn: sqlite3.Connection, prefix: str, book: list[dict] | None = None) -> dict:
    """Prefix is 'paper' or 'live'. Returns headline tiles.

    ``book`` may be passed in by the caller — typically the request handler
    that *also* hands the same list to the template as the ``book`` context
    var. Sharing the list avoids a race where two ``book_rich`` calls (one
    here for the KPI Unrealized tile, one for the Current Book table) read
    ``live_ltp`` either side of a worker write and end up reporting two
    different unrealized values for the same render.
    """
    # Headline "cumulative realized" excludes non-broker charges — those
    # surface as a Trade Log footer total, not in the tile math. Keeps the
    # tile consistent with paper.engine.compute_daily_pnl and the per-day
    # Trade Log replay below.
    if prefix == "paper":
        from app.paper.engine import _cost_basis_realized_per_session

        per_day = _cost_basis_realized_per_session(conn)
        realized_total = float(sum(per_day.values()))
    else:
        # Live keeps the SELL-notional approximation for now — live
        # realized comes from the Dhan reconciliation path which uses
        # snapshot deltas, not paper-style avg-cost replay.
        fills = conn.execute(
            f"SELECT side, fill_qty, fill_price FROM {prefix}_fills"
        ).fetchall()
        realized_total = 0.0
        for r in fills:
            if r["side"] == "SELL":
                realized_total += r["fill_qty"] * r["fill_price"]

    # `today_realized` is realized P&L from this session only — read the row
    # for today's session_date specifically, not just "latest". Otherwise a
    # day with no fills at all (worker not yet refreshed today's row) would
    # read yesterday's realized as if it were today's.
    sess_today = session_date_for(now_ist())
    today_row = conn.execute(
        f"SELECT realized FROM {prefix}_pnl_daily WHERE session_date = ?",
        (sess_today.isoformat(),),
    ).fetchone()
    today_realized = float(today_row["realized"]) if today_row else 0.0

    # `today_unrealized` is the **running** open-position MTM (vs. avg_cost),
    # computed live from the same `book_rich` snapshot the Current Book table
    # uses — same number on tile and table, no live_ltp race.
    if book is None:
        book = book_rich(conn, prefix)
    today_unrealized = sum(float(b["unrealized_pnl"]) for b in book)

    # `today_change` is the headline "what did the portfolio do *today*"
    # number. Equals today's realized + (current unrealized − yesterday's
    # EOD unrealized). The subtraction matters: a position carried over from
    # yesterday already had a running unrealized at yesterday's close, and
    # that piece is yesterday's P&L, not today's. Earlier this read
    # `today_realized + today_unrealized` (running) which double-counted
    # carried-over unrealized as today's P&L — flagged by the user when a
    # buy-and-hold day with zero price move read −₹178.86 instead of ~₹0.
    prior = conn.execute(
        f"SELECT unrealized FROM {prefix}_pnl_daily"
        " WHERE session_date < ? ORDER BY session_date DESC LIMIT 1",
        (sess_today.isoformat(),),
    ).fetchone()
    prior_eod_unrealized = float(prior["unrealized"]) if prior else 0.0
    today_change = today_realized + (today_unrealized - prior_eod_unrealized)

    today = {
        "realized": today_realized,
        "unrealized": today_unrealized,           # running, for Unrealized tile
        "mtm": today_change,                       # intraday delta, for Today's Change
        "prior_eod_unrealized": prior_eod_unrealized,
    }

    # `book` (the position list) is the in-memory snapshot we rely on for
    # the live PV tile below — keep it untouched. Use a separate name for
    # the position count.
    if prefix == "paper":
        open_count = conn.execute("SELECT COUNT(*) AS c FROM paper_book").fetchone()["c"]
    else:
        open_count = (
            conn.execute(
                "SELECT COUNT(DISTINCT symbol) AS c FROM live_positions_snapshot"
                " WHERE taken_at = (SELECT MAX(taken_at) FROM live_positions_snapshot)"
            ).fetchone()["c"]
            or 0
        )

    closed = conn.execute(f"SELECT COUNT(*) AS c FROM {prefix}_fills").fetchone()["c"]
    out = {
        "today_mtm": today["mtm"],
        "today_realized": today["realized"],
        "today_unrealized": today["unrealized"],
        "cumulative": realized_total + today["unrealized"],
        "open_positions": int(open_count),
        "closed_fills": int(closed),
    }
    # Headline value tile: how big is the book *right now*.
    if prefix == "paper":
        # Paper side has explicit cash tracking, so we report full PV
        # (cash + Σ qty × marked_price).
        from app.paper.engine import paper_portfolio_value

        out["portfolio_value"] = paper_portfolio_value(conn)
        out["portfolio_value_label"] = "Portfolio Value"
    else:
        # Live side: cash sits in the Dhan account and the web process
        # can't fetch it (FRD B.2 — only the worker calls Dhan). Report
        # holdings MTM instead, labelled to make the distinction obvious.
        # Equals Σ(qty × marked_price) for the latest live snapshot.
        out["portfolio_value"] = sum(float(b["qty"]) * float(b["marked_price"]) for b in book)
        out["portfolio_value_label"] = "Holdings Value"
    return out


def paper_summary(conn: sqlite3.Connection) -> dict:
    return _summary(conn, "paper")


def live_summary(conn: sqlite3.Connection) -> dict:
    return _summary(conn, "live")


def execution_done_today(conn: sqlite3.Connection, session_date: date) -> bool:
    """True iff ``sessions.execution_completed_at`` is set for today.

    Used by the UI to disable the manual "Refresh" button — the idempotency
    guard (FRD B.13) blocks re-runs on the same session anyway, so making
    the button obviously inert avoids spamming rejected-sentinel alerts.
    """
    row = conn.execute(
        "SELECT execution_completed_at FROM sessions WHERE session_date = ?",
        (session_date.isoformat(),),
    ).fetchone()
    return bool(row and row["execution_completed_at"])


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
    latest = conn.execute(
        "SELECT MAX(taken_at) AS t FROM live_positions_snapshot"
    ).fetchone()["t"]
    if not latest:
        return []
    rows = conn.execute(
        "SELECT symbol, qty, avg_cost, ltp, unrealized FROM live_positions_snapshot"
        " WHERE taken_at = ? ORDER BY symbol",
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


# --------------------------------------------------------------------------
# Paper-tab rich view-model.
# --------------------------------------------------------------------------


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=IST)
    return d.astimezone(IST)


def _fmt_ts(d: datetime | None) -> str:
    if d is None:
        return "—"
    return d.strftime("%Y-%m-%d %H:%M")


def _fmt_date_short(d: datetime) -> str:
    """e.g. '24 Apr 26, 09:30 am'."""
    hh = d.strftime("%I").lstrip("0") or "12"
    return d.strftime(f"%d %b %y, {hh}:%M %p").replace("AM", "am").replace("PM", "pm")


def _fmt_inr(n: float, decimals: int = 2) -> str:
    """Indian-style grouping (12,34,567.89)."""
    neg = n < 0
    n = abs(n)
    whole, frac = f"{n:.{decimals}f}".split(".") if decimals else (f"{int(round(n))}", "")
    if len(whole) <= 3:
        out = whole
    else:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join(
            [head[max(0, i - 2) : i] for i in range(len(head), 0, -2)][::-1]
        )
        out = f"{head},{tail}"
    if decimals:
        out = f"{out}.{frac}"
    return f"-{out}" if neg else out


def paper_meta(conn: sqlite3.Connection, session_date: date) -> dict:
    return book_meta(conn, session_date, "paper")


def book_meta(conn: sqlite3.Connection, session_date: date, prefix: str = "paper") -> dict:
    """Sub-header facts: book start, session start, last-updated.

    `prefix` is 'paper' or 'live'. For live, the "book" is derived from the
    latest live_positions_snapshot row rather than a dedicated table.
    """
    if prefix == "paper":
        book_start_row = conn.execute(
            "SELECT MIN(updated_at) AS t FROM paper_book"
        ).fetchone()
    else:
        book_start_row = conn.execute(
            "SELECT MIN(taken_at) AS t FROM live_positions_snapshot"
        ).fetchone()
    first_fill_row = conn.execute(
        f"SELECT MIN(filled_at) AS t FROM {prefix}_fills"
    ).fetchone()
    book_start = _parse_ts((book_start_row or {}).get("t") if isinstance(book_start_row, dict)
                           else book_start_row["t"] if book_start_row else None) \
        or _parse_ts(first_fill_row["t"] if first_fill_row else None)

    session_start_dt = datetime.combine(session_date, time(0, 0), tzinfo=IST)

    cand_sqls = [
        f"SELECT MAX(computed_at) AS t FROM {prefix}_pnl_daily",
        f"SELECT MAX(filled_at) AS t FROM {prefix}_fills",
        "SELECT MAX(fetched_at) AS t FROM live_ltp",
    ]
    if prefix == "paper":
        cand_sqls.append("SELECT MAX(updated_at) AS t FROM paper_book")
    else:
        cand_sqls.append("SELECT MAX(taken_at) AS t FROM live_positions_snapshot")

    cands: list[datetime | None] = []
    for sql in cand_sqls:
        try:
            row = conn.execute(sql).fetchone()
            cands.append(_parse_ts(row["t"]) if row and row["t"] else None)
        except sqlite3.OperationalError:
            continue
    last_updated = max((c for c in cands if c is not None), default=None)

    return {
        "book_start": _fmt_ts(book_start) if book_start else "—",
        "session_start": _fmt_ts(session_start_dt),
        "last_updated": _fmt_ts(last_updated) if last_updated else "—",
    }


def _chronological_paper_fills(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _chronological_fills(conn, "paper")


def _chronological_fills(conn: sqlite3.Connection, prefix: str = "paper") -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT id, session_date, symbol, side, fill_qty, fill_price, charges_total,"
        f" charges_json, filled_at FROM {prefix}_fills ORDER BY filled_at, id"
    ).fetchall()


def _replay_closed_trades(fills: list[sqlite3.Row]) -> list[dict]:
    """Walk fills in time order. A SELL closes qty against the running avg
    cost of that symbol. Returns one row per SELL with realized P&L (gross
    of non-broker charges — those are reported separately in the trade-log
    footer so the headline number tracks pure price gain/loss).

    Uses avg-cost method (same as paper_book) so the realized figure the
    UI shows matches the book's cost_basis accounting.
    """
    running: dict[str, dict] = {}  # symbol -> {qty, cost_basis}
    closed: list[dict] = []
    for f in fills:
        sym = f["symbol"]
        state = running.setdefault(sym, {"qty": 0, "cost_basis": 0.0})
        if f["side"] == "BUY":
            state["qty"] += int(f["fill_qty"])
            state["cost_basis"] += float(f["fill_qty"]) * float(f["fill_price"])
        else:  # SELL
            qty = int(f["fill_qty"])
            if state["qty"] <= 0:
                # Shouldn't happen in momentum long-only strategy; skip.
                continue
            avg_cost = state["cost_basis"] / state["qty"] if state["qty"] else 0.0
            pnl_gross = (float(f["fill_price"]) - avg_cost) * qty
            closed.append(
                {
                    "session_date": f["session_date"],
                    "symbol": sym,
                    "qty": qty,
                    "avg_cost": avg_cost,
                    "fill_price": float(f["fill_price"]),
                    "pnl_gross": pnl_gross,
                    "pnl_net": pnl_gross,  # retained for back-compat; equals gross now
                    "filled_at": f["filled_at"],
                    "return_pct": (pnl_gross / (avg_cost * qty) * 100.0) if avg_cost else 0.0,
                }
            )
            # Reduce state.
            state["cost_basis"] -= avg_cost * qty
            state["qty"] -= qty
            if state["qty"] == 0:
                state["cost_basis"] = 0.0
    return closed


def paper_summary_rich(conn: sqlite3.Connection) -> dict:
    return summary_rich(conn, "paper")


def live_summary_rich(conn: sqlite3.Connection) -> dict:
    return summary_rich(conn, "live")


def summary_rich(conn: sqlite3.Connection, prefix: str = "paper", book: list[dict] | None = None) -> dict:
    """Full KPI set including win-rate, profit factor, drawdown, etc.

    Returns a superset of `_summary(prefix)`. Safe to compute on an empty DB.
    Accepts 'paper' or 'live' to drive the same UI off either book.

    ``book`` is the per-request `book_rich` list — see `_summary` for why
    sharing it across the call avoids a `live_ltp` race between the KPI
    tile and the Current Book table.
    """
    base = _summary(conn, prefix, book=book)
    fills = _chronological_fills(conn, prefix)
    closed = _replay_closed_trades(fills)

    pnl_series = pnl_timeseries(conn, prefix, limit=10000)

    n = len(closed)
    wins = [c for c in closed if c["pnl_net"] > 0]
    losses = [c for c in closed if c["pnl_net"] < 0]
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    loss_rate = (len(losses) / n * 100.0) if n else 0.0
    avg_pnl = (sum(c["pnl_net"] for c in closed) / n) if n else 0.0
    avg_win = (sum(c["pnl_net"] for c in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(c["pnl_net"] for c in losses) / len(losses)) if losses else 0.0
    best = max((c["pnl_net"] for c in closed), default=0.0)
    worst = min((c["pnl_net"] for c in closed), default=0.0)
    sum_wins = sum(c["pnl_net"] for c in wins)
    sum_losses_abs = -sum(c["pnl_net"] for c in losses)
    profit_factor = (sum_wins / sum_losses_abs) if sum_losses_abs > 1e-9 else (
        float("inf") if sum_wins > 0 else 0.0
    )
    expectancy = avg_pnl  # per-trade expectancy; same as avg_pnl for a flat sample

    # Drawdown over cumulative MTM sequence.
    cum = []
    acc = 0.0
    for p in pnl_series:
        acc += float(p["mtm"])
        cum.append(acc)
    max_dd = 0.0
    peak = 0.0
    dd_started_at = None
    dd_len = 0
    current_dd_len = 0
    max_dd_len = 0
    for v in cum:
        if v >= peak:
            peak = v
            current_dd_len = 0
        else:
            current_dd_len += 1
            draw = v - peak
            if draw < max_dd:
                max_dd = draw
                max_dd_len = current_dd_len
    max_drawdown = -max_dd  # positive number for display

    # Streaks.
    max_win_streak = 0
    max_loss_streak = 0
    cur_w = cur_l = 0
    for c in closed:
        if c["pnl_net"] > 0:
            cur_w += 1
            cur_l = 0
            max_win_streak = max(max_win_streak, cur_w)
        elif c["pnl_net"] < 0:
            cur_l += 1
            cur_w = 0
            max_loss_streak = max(max_loss_streak, cur_l)
        else:
            cur_w = cur_l = 0

    overall_profit = base["cumulative"]
    return_over_maxdd = (overall_profit / max_drawdown) if max_drawdown > 1e-9 else (
        float("inf") if overall_profit > 0 else 0.0
    )
    reward_to_risk = (avg_win / -avg_loss) if avg_loss < -1e-9 else (
        float("inf") if avg_win > 0 else 0.0
    )

    def _safe(n: float) -> float:
        """Clamp infinities to a display sentinel (0.0) and NaN to 0.0 so
        Jinja number formatters never receive values they can't render."""
        if n != n or n in (float("inf"), float("-inf")):
            return 0.0
        return n

    def _is_inf(n: float) -> bool:
        return n in (float("inf"), float("-inf"))

    base.update(
        {
            "today_change": base["today_mtm"],
            "closed_trades": n,
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": win_rate,
            "loss_rate": loss_rate,
            "avg_pnl_per_closed": avg_pnl,
            "avg_profit_winning": avg_win,
            "avg_loss_losing": avg_loss,
            "best_trade": best,
            "worst_trade": worst,
            "profit_factor": _safe(profit_factor),
            "profit_factor_is_inf": _is_inf(profit_factor),
            "max_drawdown": max_drawdown,
            "max_drawdown_duration_days": max_dd_len,
            "return_over_maxdd": _safe(return_over_maxdd),
            "return_over_maxdd_is_inf": _is_inf(return_over_maxdd),
            "reward_to_risk": _safe(reward_to_risk),
            "reward_to_risk_is_inf": _is_inf(reward_to_risk),
            "expectancy": expectancy,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "overall_profit": overall_profit,
        }
    )
    return base


def today_status(conn: sqlite3.Connection, session_date: date, prefix: str = "paper") -> dict:
    """One-row summary of today's session, for the Today Status band.

    `prefix` is 'paper' or 'live'. Live book comes from the latest
    live_positions_snapshot rather than a dedicated table.
    """
    if prefix == "paper":
        book = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(cost_basis),0) AS invest, MAX(updated_at) AS t"
            " FROM paper_book"
        ).fetchone()
    else:
        # Live book: sum qty*avg_cost on the latest snapshot.
        book = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(qty*avg_cost),0) AS invest, MAX(taken_at) AS t"
            " FROM live_positions_snapshot"
            " WHERE taken_at = (SELECT MAX(taken_at) FROM live_positions_snapshot)"
        ).fetchone()
    today_pnl = conn.execute(
        f"SELECT realized, unrealized, mtm, computed_at FROM {prefix}_pnl_daily WHERE session_date = ?",
        (session_date.isoformat(),),
    ).fetchone()
    today_buys = conn.execute(
        f"SELECT COUNT(*) AS c FROM {prefix}_fills WHERE session_date = ? AND side = 'BUY'",
        (session_date.isoformat(),),
    ).fetchone()["c"]

    legs = int(book["c"]) if book else 0
    invest = float(book["invest"]) if book else 0.0
    mtm = float(today_pnl["mtm"]) if today_pnl else 0.0
    realized = float(today_pnl["realized"]) if today_pnl else 0.0
    unreal = float(today_pnl["unrealized"]) if today_pnl else 0.0
    ret_pct = (mtm / invest * 100.0) if invest > 1e-9 else 0.0
    marked_at = _parse_ts(
        (today_pnl["computed_at"] if today_pnl else None) or (book["t"] if book else None)
    )

    trade_opened = bool(today_buys)
    status = "Open" if legs > 0 else ("Closed" if trade_opened else "Pending")
    exit_type = "Open Position" if legs > 0 else ("Closed" if trade_opened else "—")

    return {
        "index": "BSE",
        "date": session_date.isoformat(),
        "trade_slot": "Primary",
        "status": status,
        "trade_opened": trade_opened,
        "open_legs": legs,
        "investment_rs": invest,
        "today_mtm": mtm,
        "realized_rs": realized,
        "open_unrealized_pnl": unreal,
        "return_pct": ret_pct,
        "exit_type": exit_type,
        "marked_at": _fmt_ts(marked_at) if marked_at else "—",
    }


def performance_summary(conn: sqlite3.Connection, prefix: str = "paper") -> list[list[dict]]:
    """3-column metric grid matching the reference screenshot.

    `prefix` is 'paper' or 'live'.
    """
    s = summary_rich(conn, prefix)

    def inr(n: float, d: int = 2) -> str:
        if n != n or n in (float("inf"), float("-inf")):
            return "inf" if n > 0 else "-inf"
        return f"Rs {_fmt_inr(n, d)}"

    def pct(n: float) -> str:
        if n != n or n in (float("inf"), float("-inf")):
            return "inf" if n > 0 else "-inf"
        return f"{n:.2f}%"

    def num(n: float, d: int = 2) -> str:
        if n != n or n in (float("inf"), float("-inf")):
            return "inf" if n > 0 else "-inf"
        return f"{n:.{d}f}"

    def signed(n: float) -> str:
        """Tone for a value whose sign carries P&L meaning. Zero is neutral
        so a quiet "no trades closed" row doesn't flash green."""
        if n != n:
            return ""
        if n > 0:
            return "pos"
        if n < 0:
            return "neg"
        return ""

    def good_when_positive(n: float) -> str:
        """For metrics that are bounded non-negative but sentiment-positive
        when > 0 (Win %, avg winning, best trade in a fully-winning book)."""
        if n != n or n <= 0:
            return ""
        return "pos"

    def bad_when_positive(n: float) -> str:
        """For metrics that are bounded non-negative but sentiment-negative
        when > 0 (Loss %, Max Drawdown, |avg losing|)."""
        if n != n or n <= 0:
            return ""
        return "neg"

    def ratio_tone(n: float, is_inf: bool) -> str:
        """Return / MaxDD and Reward / Risk: positive ratio = good, negative
        = bad, inf = pos (only happens when MaxDD is zero and overall_profit
        > 0, which is the best possible state)."""
        if is_inf:
            return "pos"
        return signed(n)

    label_prefix = "Paper" if prefix == "paper" else "Live"
    col1 = [
        {"metric": f"Overall {label_prefix} Profit", "value": inr(s["overall_profit"]), "tone": signed(s["overall_profit"])},
        {"metric": "Closed Trades", "value": str(s["closed_trades"]), "tone": ""},
        {"metric": "Average P&L Per Closed Trade", "value": inr(s["avg_pnl_per_closed"]), "tone": signed(s["avg_pnl_per_closed"])},
        {"metric": "Win %", "value": pct(s["win_rate"]), "tone": good_when_positive(s["win_rate"])},
        {"metric": "Loss %", "value": pct(s["loss_rate"]), "tone": bad_when_positive(s["loss_rate"])},
        {"metric": "Average Profit on Winning Trades", "value": inr(s["avg_profit_winning"]), "tone": good_when_positive(s["avg_profit_winning"])},
    ]
    col2 = [
        {"metric": "Average Loss on Losing Trades", "value": inr(s["avg_loss_losing"]), "tone": signed(s["avg_loss_losing"])},
        {"metric": "Best Trade", "value": inr(s["best_trade"]), "tone": signed(s["best_trade"])},
        {"metric": "Worst Trade", "value": inr(s["worst_trade"]), "tone": signed(s["worst_trade"])},
        # max_drawdown is stored as a positive number representing magnitude
        # of loss, so "drawdown > 0" means "we drew down" → red.
        {"metric": "Max Drawdown", "value": inr(s["max_drawdown"]), "tone": bad_when_positive(s["max_drawdown"])},
        {"metric": "Duration of Max Drawdown", "value": str(s["max_drawdown_duration_days"]), "tone": ""},
        {"metric": "Open Positions Right Now", "value": str(s["open_positions"]), "tone": ""},
    ]
    return_over_maxdd_str = "inf" if s.get("return_over_maxdd_is_inf") else num(s["return_over_maxdd"])
    reward_to_risk_str = "inf" if s.get("reward_to_risk_is_inf") else num(s["reward_to_risk"])
    col3 = [
        {"metric": "Return / MaxDD", "value": return_over_maxdd_str, "tone": ratio_tone(s["return_over_maxdd"], s.get("return_over_maxdd_is_inf", False))},
        {"metric": "Reward to Risk Ratio", "value": reward_to_risk_str, "tone": ratio_tone(s["reward_to_risk"], s.get("reward_to_risk_is_inf", False))},
        {"metric": "Expectancy / Closed Trade", "value": inr(s["expectancy"]), "tone": signed(s["expectancy"])},
        {"metric": "Max Win Streak (trades)", "value": str(s["max_win_streak"]), "tone": good_when_positive(s["max_win_streak"])},
        {"metric": "Max Losing Streak (trades)", "value": str(s["max_loss_streak"]), "tone": bad_when_positive(s["max_loss_streak"])},
        {"metric": "Max Days in Drawdown", "value": str(s["max_drawdown_duration_days"]), "tone": bad_when_positive(s["max_drawdown_duration_days"])},
    ]
    return [col1, col2, col3]


def signals_today_brief(conn: sqlite3.Connection, session_date: date) -> list[dict]:
    """Short signal list for the top-right table: Action, Symbol, Target Qty,
    Signal Generated At, Trade At. `selected=1` rows only."""
    rows = conn.execute(
        "SELECT symbol, target_qty FROM signals WHERE session_date = ? AND selected = 1"
        " ORDER BY rank_by_126d",
        (session_date.isoformat(),),
    ).fetchall()
    # Signal is stamped at 09:10 IST, trades at 09:30 IST (FRD A.3 / A.5).
    signal_at = datetime.combine(session_date, time(9, 10), tzinfo=IST).strftime("%Y-%m-%d %H:%M")
    trade_at = datetime.combine(session_date, time(9, 30), tzinfo=IST).strftime("%Y-%m-%d %H:%M")
    return [
        {
            "action": "BUY",
            "symbol": r["symbol"],
            "target_qty": r["target_qty"],
            "signal_generated_at": signal_at,
            "trade_at": trade_at,
        }
        for r in rows
    ]


def paper_book_rich(conn: sqlite3.Connection) -> list[dict]:
    return book_rich(conn, "paper")


def live_book_rich(conn: sqlite3.Connection) -> list[dict]:
    return book_rich(conn, "live")


def book_rich(conn: sqlite3.Connection, prefix: str = "paper") -> list[dict]:
    """Current book with marked price, market value, unrealized P&L.

    Marked price priority: live_ltp (fresh) → last-known fill price for the
    symbol → avg_cost (falls back to 0 unrealized when nothing else is known).

    For `prefix="live"` the "book" is derived from the latest
    live_positions_snapshot and cost_basis is qty * avg_cost.
    """
    if prefix == "paper":
        book = conn.execute(
            "SELECT symbol, qty, avg_cost, cost_basis, updated_at FROM paper_book ORDER BY symbol"
        ).fetchall()
    else:
        book = conn.execute(
            "SELECT symbol, qty, avg_cost, (qty*avg_cost) AS cost_basis, taken_at AS updated_at"
            " FROM live_positions_snapshot"
            " WHERE taken_at = (SELECT MAX(taken_at) FROM live_positions_snapshot)"
            " ORDER BY symbol"
        ).fetchall()
    if not book:
        return []

    # Prefetch latest fill + signal timestamps + LTPs for all open symbols.
    ltp_map: dict[str, dict] = {}
    try:
        for r in conn.execute("SELECT symbol, ltp, fetched_at FROM live_ltp"):
            ltp_map[r["symbol"]] = {"ltp": float(r["ltp"]), "fetched_at": r["fetched_at"]}
    except sqlite3.OperationalError:
        pass

    last_buy: dict[str, dict] = {}
    for r in conn.execute(
        f"SELECT symbol, fill_price, filled_at FROM {prefix}_fills WHERE side='BUY' ORDER BY filled_at"
    ):
        last_buy[r["symbol"]] = {"fill_price": float(r["fill_price"]), "filled_at": r["filled_at"]}

    signal_at_map: dict[str, str] = {}
    for r in conn.execute(
        "SELECT symbol, MIN(session_date) AS first FROM signals WHERE selected=1 GROUP BY symbol"
    ):
        # Signals stamped at 09:10 IST on the session_date.
        try:
            d = date.fromisoformat(r["first"])
            signal_at_map[r["symbol"]] = (
                datetime.combine(d, time(9, 10), tzinfo=IST).strftime("%Y-%m-%d %H:%M")
            )
        except Exception:  # noqa: BLE001
            signal_at_map[r["symbol"]] = "—"

    out = []
    for b in book:
        sym = b["symbol"]
        ltp_info = ltp_map.get(sym)
        fill_info = last_buy.get(sym)
        marked_price = None
        marked_at = None
        if ltp_info is not None:
            marked_price = ltp_info["ltp"]
            marked_at = ltp_info["fetched_at"]
        elif fill_info is not None:
            marked_price = fill_info["fill_price"]
            marked_at = fill_info["filled_at"]
        else:
            marked_price = float(b["avg_cost"])
            marked_at = b["updated_at"]

        qty = int(b["qty"])
        avg_cost = float(b["avg_cost"])
        market_value = marked_price * qty
        unrealized = (marked_price - avg_cost) * qty
        unrealized_pct = ((marked_price - avg_cost) / avg_cost * 100.0) if avg_cost else 0.0

        trade_at = "—"
        if fill_info and fill_info["filled_at"]:
            d = _parse_ts(fill_info["filled_at"])
            if d:
                trade_at = d.strftime("%Y-%m-%d %H:%M")

        out.append(
            {
                "signal_at": signal_at_map.get(sym, "—"),
                "trade_at": trade_at,
                "symbol": sym,
                "qty": qty,
                "avg_cost": avg_cost,
                "cost_basis": float(b["cost_basis"]),
                "marked_price": marked_price,
                "market_value": market_value,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": unrealized_pct,
                "marked_at": _fmt_ts(_parse_ts(marked_at)) if marked_at else "—",
                "is_stale": ltp_info is None,  # marks based on fill/avg, not live
            }
        )
    return out


def day_grouped_trade_log(conn: sqlite3.Connection, limit_days: int = 30, prefix: str = "paper") -> list[dict]:
    """Trades grouped by session_date with running portfolio value and
    per-fill details. Matches the reference screenshot's 'Trade log'.

    `prefix` is 'paper' or 'live'.
    """
    fills = conn.execute(
        f"SELECT id, session_date, symbol, side, fill_qty, fill_price, charges_total,"
        f" charges_json, filled_at FROM {prefix}_fills ORDER BY filled_at, id"
    ).fetchall()
    if not fills:
        return []

    # Replay for per-SELL realized P&L. P&L is GROSS of non-broker charges —
    # those surface in a per-day footer total so the headline cell tracks
    # pure price gain/loss.
    closed_by_fill_id: dict[int, dict] = {}
    # Per-fill kind labels — replayed from the running cost-basis book so the
    # Trade Log can call a SELL "Full Exit" only when it actually closes the
    # position, and a BUY "Top-up" when it adds to an existing leg. The old
    # version hardcoded every SELL as "Partial Exit" and every BUY as
    # "New Entry", which read as a bug the moment a full rotation hit.
    kind_by_fill_id: dict[int, str] = {}
    running: dict[str, dict] = {}
    for f in fills:
        sym = f["symbol"]
        st = running.setdefault(sym, {"qty": 0, "cost_basis": 0.0})
        if f["side"] == "BUY":
            had_position = st["qty"] > 0
            st["qty"] += int(f["fill_qty"])
            st["cost_basis"] += float(f["fill_qty"]) * float(f["fill_price"])
            kind_by_fill_id[int(f["id"])] = "Top-up" if had_position else "New Entry"
        else:
            qty = int(f["fill_qty"])
            if st["qty"] <= 0:
                continue
            avg = st["cost_basis"] / st["qty"] if st["qty"] else 0.0
            pnl = (float(f["fill_price"]) - avg) * qty
            ret_pct = (pnl / (avg * qty) * 100.0) if avg else 0.0
            closed_by_fill_id[int(f["id"])] = {"pnl": pnl, "ret_pct": ret_pct, "avg": avg}
            st["cost_basis"] -= avg * qty
            st["qty"] -= qty
            if st["qty"] == 0:
                st["cost_basis"] = 0.0
                kind_by_fill_id[int(f["id"])] = "Full Exit"
            else:
                kind_by_fill_id[int(f["id"])] = "Partial Exit"

    # Running cumulative P&L (for portfolio value per day).
    opening_capital = 100_000.0  # display default; reference screenshot uses 1L seed

    days_order: list[str] = []
    day_rows: dict[str, list[dict]] = defaultdict(list)
    running_pnl_to_end_of_day: dict[str, float] = {}
    per_day_pnl: dict[str, float] = defaultdict(float)
    per_day_non_broker: dict[str, float] = defaultdict(float)

    for f in fills:
        sd = f["session_date"]
        if sd not in day_rows:
            days_order.append(sd)
        try:
            ch = json.loads(f["charges_json"])
            nb = round(ch["total"] - ch["brokerage"], 4)
        except Exception:  # noqa: BLE001
            nb = None

        closed = closed_by_fill_id.get(int(f["id"]))
        entry_at = _parse_ts(f["filled_at"])
        row: dict[str, Any] = {
            "symbol": f["symbol"],
            "side": f["side"],
            "badge": "Rebalance",
            "kind": kind_by_fill_id.get(int(f["id"]), "Partial Exit" if f["side"] == "SELL" else "New Entry"),
            "entry_at": _fmt_date_short(entry_at) if entry_at and f["side"] == "BUY" else "—",
            "exit_at": _fmt_date_short(entry_at) if entry_at and f["side"] == "SELL" else "—",
            "fill_price": float(f["fill_price"]),
            "order_qty": int(f["fill_qty"]) if f["side"] == "BUY" else -int(f["fill_qty"]),
            "fill_qty": int(f["fill_qty"]) if f["side"] == "BUY" else -int(f["fill_qty"]),
            "profit_loss": closed["pnl"] if closed else None,
            "returns_pct": closed["ret_pct"] if closed else None,
            "non_broker_charges": nb,
        }
        day_rows[sd].append(row)
        if closed:
            per_day_pnl[sd] += closed["pnl"]
        if nb is not None:
            per_day_non_broker[sd] += nb

    cum = 0.0
    for sd in days_order:
        cum += per_day_pnl.get(sd, 0.0)
        running_pnl_to_end_of_day[sd] = cum

    # End-of-day unrealized per session, sourced from {prefix}_pnl_daily
    # (paper kept fresh by paper_mtm_refresh_job, FRD B.5/B.6; live
    # populated by the recon path's compute_live_daily_pnl). Including
    # this in the portfolio-value column means a buy-and-hold day still
    # reflects the open book's mark-to-market gain instead of sticking
    # at the seed.
    unrealized_eod: dict[str, float] = {
        r["session_date"]: float(r["unrealized"])
        for r in conn.execute(f"SELECT session_date, unrealized FROM {prefix}_pnl_daily")
    }

    # Build collapsible groups in reverse-chronological order (newest first).
    groups = []
    for sd in reversed(days_order[-limit_days:]):
        label_dt = datetime.fromisoformat(sd).replace(tzinfo=IST)
        pv = (
            opening_capital
            + running_pnl_to_end_of_day[sd]
            + unrealized_eod.get(sd, 0.0)
        )
        nb_total = per_day_non_broker.get(sd, 0.0)
        groups.append(
            {
                "session_date": sd,
                "label": f"Trades on {label_dt.strftime('%d %b %y')}, 09:30 am",
                "portfolio_value": pv,
                "portfolio_value_str": f"Rs {_fmt_inr(pv)}",
                "rows": day_rows[sd],
                "non_broker_charges_total": nb_total,
                "non_broker_charges_str": f"Rs {nb_total:,.2f}",
            }
        )
    return groups


# --------------------------------------------------------------------------
# Top-bar + auth helpers (unchanged below).
# --------------------------------------------------------------------------


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
