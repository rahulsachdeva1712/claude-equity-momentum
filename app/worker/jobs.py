"""Worker jobs: signal, execution, recon, token watchers. FRD B.5."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from app.alerts import Alert, raise_alert
from app.db import connect, tx
from app.dhan.client import DhanClient, jwt_seconds_to_expiry
from app.dhan.errors import DhanUnavailable
from app.dhan.models import OHLCBar
from app.live.engine import is_live_enabled, place_orders
from app.live.recon import compute_live_daily_pnl, snapshot_positions
from app.paper.engine import (
    compute_daily_pnl as compute_paper_pnl,
    execute_orders as paper_execute,
    generate_orders as paper_generate,
)
from app.paths import artifact_file, env_file
from app.strategy.config import DEFAULT_CONFIG
from app.strategy.signals import build_target_set, compute_universe_metrics
from app.strategy.universe import UniverseEntry, UniverseProvider, default_provider
from app.time_utils import now_ist, session_date_for

log = logging.getLogger("jobs")


def _bars_to_panel(bars_by_symbol: dict[str, list[OHLCBar]], universe: list[UniverseEntry]) -> pd.DataFrame:
    by_sec: dict[str, UniverseEntry] = {u.security_id: u for u in universe}
    rows = []
    for sym_id, bars in bars_by_symbol.items():
        meta = by_sec.get(sym_id)
        if meta is None:
            continue
        for b in bars:
            rows.append(
                {
                    "symbol": meta.symbol,
                    "security_id": meta.security_id,
                    "exchange_segment": meta.exchange_segment,
                    "market_cap_cr": meta.market_cap_cr,
                    "date": pd.Timestamp(b.ts.date()),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
            )
    return pd.DataFrame(rows)


async def _market_is_open(dhan: DhanClient, conn: sqlite3.Connection) -> bool:
    """FRD B.5: query Dhan at trigger time, alert + skip if closed."""
    try:
        status = await dhan.market_status()
    except DhanUnavailable as e:
        raise_alert(conn, Alert(severity="warn", source="jobs", message=f"market_status unavailable: {e}"))
        return False
    if status != "OPEN":
        raise_alert(
            conn,
            Alert(severity="info", source="jobs", message=f"market not open (status={status}); skipping job"),
        )
        return False
    return True


async def signal_job(
    dhan: DhanClient,
    provider: UniverseProvider | None = None,
    capital_override: float | None = None,
) -> None:
    """09:10 IST: compute signal set and stage paper orders. FRD A.6, A.7, B.5."""
    provider = provider or default_provider()
    conn = connect()
    try:
        if not await _market_is_open(dhan, conn):
            return

        universe = provider.load()
        if not universe:
            raise_alert(conn, Alert(severity="warn", source="jobs", message="empty universe; skip"))
            return

        sess = session_date_for(now_ist())
        from_date = (sess - timedelta(days=400)).isoformat()
        to_date = sess.isoformat()

        bars_by_sec: dict[str, list[OHLCBar]] = {}
        for u in universe:
            try:
                bars = await dhan.historical_daily(u.security_id, u.exchange_segment, from_date, to_date)
            except DhanUnavailable as e:
                log.warning("historical fetch failed for %s: %s", u.symbol, e)
                continue
            if bars:
                bars_by_sec[u.security_id] = bars

        panel = _bars_to_panel(bars_by_sec, universe)
        if panel.empty:
            raise_alert(conn, Alert(severity="warn", source="jobs", message="no historical data; skip"))
            return

        metrics = compute_universe_metrics(panel, DEFAULT_CONFIG)
        capital = capital_override if capital_override is not None else _capital_for(conn)
        target = build_target_set(metrics, sess, capital=capital)

        with tx(conn):
            conn.execute("DELETE FROM signals WHERE session_date = ?", (sess.isoformat(),))
            for r in target.rows:
                conn.execute(
                    "INSERT INTO signals"
                    " (session_date, symbol, security_id, exchange_segment, selected,"
                    "  rank_by_126d, target_weight, target_qty, reference_price)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sess.isoformat(),
                        r.symbol,
                        r.security_id,
                        r.exchange_segment,
                        1 if r.selected else 0,
                        r.rank_by_126d,
                        r.weight,
                        r.target_qty,
                        r.reference_price,
                    ),
                )
            conn.execute(
                "INSERT INTO sessions (session_date, signal_completed_at, market_open)"
                " VALUES (?, ?, 1)"
                " ON CONFLICT(session_date) DO UPDATE SET"
                " signal_completed_at=excluded.signal_completed_at, market_open=1",
                (sess.isoformat(), now_ist().isoformat()),
            )

        # Stage paper orders (09:10 side of FRD B.5)
        paper_generate(conn, sess)

        # Debug artifact (FRD B.10)
        with artifact_file(f"last_signal_{sess.isoformat()}.json").open("w") as f:
            json.dump(
                {
                    "session_date": sess.isoformat(),
                    "selected": [
                        {
                            "symbol": r.symbol,
                            "rank": r.rank_by_126d,
                            "weight": r.weight,
                            "target_qty": r.target_qty,
                            "reference_price": r.reference_price,
                        }
                        for r in target.rows
                    ],
                    "computed_at": now_ist().isoformat(),
                },
                f,
                indent=2,
            )
    finally:
        conn.close()


async def execution_job(dhan: DhanClient) -> None:
    """09:30 IST: fill paper orders at 09:30 close; if live enabled, place
    tagged Dhan orders and have paper honor the actual live qty.
    """
    conn = connect()
    try:
        if not await _market_is_open(dhan, conn):
            return

        sess = session_date_for(now_ist())
        # Guard: do not re-run after completion (FRD B.13 idempotency rule)
        done = conn.execute(
            "SELECT execution_completed_at FROM sessions WHERE session_date = ?", (sess.isoformat(),)
        ).fetchone()
        if done and done["execution_completed_at"]:
            log.info("execution already completed for %s; skipping", sess)
            return

        paper_rows = conn.execute(
            "SELECT po.id, po.symbol, po.action, po.order_qty, s.security_id, s.exchange_segment"
            " FROM paper_orders po LEFT JOIN signals s ON s.session_date = po.session_date AND s.symbol = po.symbol"
            " WHERE po.session_date = ? AND po.status = 'PENDING'",
            (sess.isoformat(),),
        ).fetchall()
        tuples = [
            (int(r["id"]), r["symbol"], r["action"], int(r["order_qty"]), r["security_id"] or "", r["exchange_segment"] or "BSE_EQ")
            for r in paper_rows
        ]

        overrides: dict[int, int] = {}
        if is_live_enabled(conn):
            placeable = [t for t in tuples if t[4]]
            overrides = await place_orders(conn, dhan, sess, placeable)

        # Pre-fetch 09:30 close for each symbol before calling the sync paper engine.
        prices = await _fetch_0930_closes(dhan, conn, sess, [t[1] for t in tuples])
        price_fetcher = lambda sym, _prices=prices: _prices.get(sym)
        paper_execute(conn, sess, price_fetcher, qty_override=overrides)

        # Use the same snapshot for end-of-execution MTM.
        compute_paper_pnl(conn, sess, ltp_fetcher=price_fetcher)

        with tx(conn):
            conn.execute(
                "UPDATE sessions SET execution_completed_at = ? WHERE session_date = ?",
                (now_ist().isoformat(), sess.isoformat()),
            )
    finally:
        conn.close()


async def recon_job(dhan: DhanClient) -> None:
    """15s cadence during market hours. FRD B.5, B.8."""
    conn = connect()
    try:
        await snapshot_positions(conn, dhan)
        compute_live_daily_pnl(conn, session_date_for(now_ist()))
    except DhanUnavailable as e:
        raise_alert(conn, Alert(severity="warn", source="recon", message=f"positions fetch failed: {e}"))
    finally:
        conn.close()


def token_watcher_job(dhan: DhanClient, state: dict) -> None:
    """Watch .env mtime and hot-reload DHAN_ACCESS_TOKEN on change."""
    p: Path = env_file()
    if not p.exists():
        return
    mtime = p.stat().st_mtime
    last = state.get("mtime")
    if last is not None and mtime == last:
        return
    state["mtime"] = mtime
    # Re-read the file and update the client.
    from dotenv import dotenv_values

    vals = dotenv_values(p)
    tok = vals.get("DHAN_ACCESS_TOKEN") or ""
    if tok:
        dhan.set_access_token(tok)
        log.info("dhan access token reloaded")


def token_expiry_monitor_job(dhan: DhanClient, state: dict) -> None:
    """60s cadence. Alert at 60 min and 10 min before JWT `exp`. Disable live on expiry."""
    token = dhan._access_token  # noqa: SLF001 — internal read is ok within package
    if not token:
        return
    secs = jwt_seconds_to_expiry(token)
    if secs is None:
        return
    conn = connect()
    try:
        if secs <= 0 and not state.get("expired_alerted"):
            raise_alert(conn, Alert(severity="error", source="auth", message="dhan token expired; live disabled"))
            from app.live.engine import set_live_enabled
            set_live_enabled(conn, False)
            state["expired_alerted"] = True
            return
        if 0 < secs <= 600 and not state.get("ten_min_alerted"):
            raise_alert(
                conn,
                Alert(severity="warn", source="auth", message=f"dhan token expires in {secs // 60} min"),
            )
            state["ten_min_alerted"] = True
        elif 600 < secs <= 3600 and not state.get("sixty_min_alerted"):
            raise_alert(
                conn,
                Alert(severity="warn", source="auth", message=f"dhan token expires in {secs // 60} min"),
            )
            state["sixty_min_alerted"] = True
        elif secs > 3600:
            # Reset flags when a fresh token is loaded.
            state["expired_alerted"] = False
            state["ten_min_alerted"] = False
            state["sixty_min_alerted"] = False
    finally:
        conn.close()


# ---- helpers ----


def _capital_for(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key = 'capital'").fetchone()
    if row:
        try:
            return float(row["value"])
        except (TypeError, ValueError):
            return 100_000.0
    return 100_000.0


async def _fetch_0930_closes(
    dhan: DhanClient,
    conn: sqlite3.Connection,
    sess: date,
    symbols: list[str],
) -> dict[str, float | None]:
    """Fetch the 09:30 minute candle close for each symbol. Returns a dict
    that the sync paper engine can consume via dict.get."""
    out: dict[str, float | None] = {}
    from_iso = f"{sess.isoformat()}T09:30:00"
    to_iso = f"{sess.isoformat()}T09:31:00"
    for sym in symbols:
        row = conn.execute(
            "SELECT security_id, exchange_segment FROM signals WHERE session_date = ? AND symbol = ?",
            (sess.isoformat(), sym),
        ).fetchone()
        if row is None or not row["security_id"]:
            out[sym] = None
            continue
        try:
            bars = await dhan.intraday(row["security_id"], row["exchange_segment"] or "BSE_EQ", 1, from_iso, to_iso)
        except DhanUnavailable as e:
            log.warning("intraday fetch failed for %s: %s", sym, e)
            out[sym] = None
            continue
        out[sym] = bars[0].close if bars else None
    return out
