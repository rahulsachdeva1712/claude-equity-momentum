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
from app.dhan.errors import DhanError, DhanUnavailable
from app.dhan.models import OHLCBar
from app.live.engine import is_live_enabled, place_orders
from app.live.recon import compute_live_daily_pnl, snapshot_positions
from app.paper.engine import (
    compute_daily_pnl as compute_paper_pnl,
    execute_orders as paper_execute,
    generate_orders as paper_generate,
)
from app.paths import artifact_file, command_inbox, env_file
from app.strategy.config import DEFAULT_CONFIG
from app.strategy.signals import (
    build_target_set,
    compute_universe_metrics,
    static_eligible_symbols,
)
from app.strategy.universe import UniverseEntry, UniverseProvider, default_provider
from app.time_utils import now_ist, session_date_for
from app.universe.refresh import refresh_universe, universe_csv_path

log = logging.getLogger("jobs")


async def universe_refresh_job() -> None:
    """Nightly refresh of ``<state_dir>/universe/universe.csv``.

    Runs the Champion B backtest universe logic (series in {A,B,T,X,XT} +
    20-day ADV >= 10,000) against the BSE bhavcopy, joined to the Dhan
    scrip master to pick up ``security_id`` for live/paper orders. The
    refresh is an I/O-bound task we run in a thread so APScheduler's event
    loop stays responsive; errors are alerted but do not crash the worker.
    """
    loop = asyncio.get_event_loop()
    conn = connect()
    try:
        try:
            path = await loop.run_in_executor(None, refresh_universe)
        except Exception as e:  # noqa: BLE001 — alert everything, don't crash the scheduler.
            raise_alert(
                conn,
                Alert(
                    severity="error",
                    source="universe",
                    message=f"universe refresh failed: {type(e).__name__}: {e}",
                ),
            )
            return
        raise_alert(
            conn,
            Alert(
                severity="info",
                source="universe",
                message=f"universe refresh complete: {path}",
            ),
        )
    finally:
        conn.close()


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


def _ist_clock_market_open(now: datetime | None = None) -> bool:
    """Conservative fallback when Dhan market-status is unreachable: treat
    Mon-Fri 09:15-15:30 IST as open. Doesn't know about exchange holidays,
    so on a holiday we'd hand the job to Dhan's order API, which rejects
    with a documented-alert error path — better than blocking all trading
    on a working day because an upstream endpoint URL drifted.
    """
    t = now or now_ist()
    if t.weekday() >= 5:
        return False
    mins = t.hour * 60 + t.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


async def _market_is_open(dhan: DhanClient, conn: sqlite3.Connection) -> bool:
    """FRD B.5: query Dhan at trigger time, alert + skip if closed.

    Dhan's market-status endpoint has been observed to 404 after an API
    rename. On any Dhan error (transport, 4xx, 5xx) we fall back to a
    local IST-clock check so a stale endpoint URL doesn't block the whole
    trading day — the error is still alerted so the user can fix the URL.
    """
    try:
        status = await dhan.market_status()
    except (DhanUnavailable, DhanError) as e:
        fallback_open = _ist_clock_market_open()
        raise_alert(
            conn,
            Alert(
                severity="warn",
                source="jobs",
                message=(
                    f"market_status unavailable ({e}); falling back to IST clock: "
                    f"{'open' if fallback_open else 'closed'}"
                ),
            ),
        )
        return fallback_open
    if status != "OPEN":
        raise_alert(
            conn,
            Alert(severity="info", source="jobs", message=f"market not open (status={status}); skipping job"),
        )
        return False
    return True


SETTINGS_KEY_MARKET_STATUS = "market_status"


async def market_status_poll_job(dhan: DhanClient) -> None:
    """Poll Dhan `/marketfeed/marketstatus` and persist the result to the
    `settings` table so the web process can render the top-bar pill without
    making API calls itself (FRD B.2 forbids web-side Dhan writes).

    Cadence: 30s always. The web side treats rows older than ~90s as stale
    and shows 'unknown'. On transport errors we deliberately do NOT update
    the row — the old value's `updated_at` naturally ages out to 'unknown',
    which is the correct user-facing state during an outage.
    """
    try:
        status = await dhan.market_status()
    except (DhanUnavailable, DhanError) as e:
        # Treat 4xx the same as 5xx/transport for polling: don't spam alerts
        # at 2/min; let the row's updated_at age out to 'unknown'. The
        # startup self-check + execution_job's alerting remain as signals.
        log.debug("market_status poll failed: %s", e)
        return
    if not status:
        return
    conn = connect()
    try:
        with tx(conn):
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (SETTINGS_KEY_MARKET_STATUS, status, now_ist().isoformat()),
            )
    finally:
        conn.close()


async def ltp_poll_job(dhan: DhanClient) -> None:
    """Poll the last-known price for each open paper-book symbol so the web
    paper tab can show a genuinely live Marked Price + Unrealized P&L.

    Writes to ``live_ltp`` (symbol, ltp, fetched_at). Uses the 09:30→09:31
    one-minute bar for the freshest minute candle Dhan will serve during
    market hours; if Dhan is unavailable the row is left untouched and the
    UI's "stale" marker kicks in (same pattern as the market-status pill).

    The job is idempotent and cheap: at most ~20 open positions → 20 HTTP
    calls per run, far under Dhan's quota.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT pb.symbol, s.security_id, s.exchange_segment"
            " FROM paper_book pb"
            " LEFT JOIN signals s ON s.symbol = pb.symbol"
            "  AND s.session_date = (SELECT MAX(session_date) FROM signals WHERE symbol = pb.symbol)"
            " WHERE pb.qty > 0"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return

    # Window = the last completed minute. Dhan returns the candle as soon as
    # the minute boundary passes.
    now = now_ist()
    to_dt = now.replace(second=0, microsecond=0)
    from_dt = to_dt - timedelta(minutes=2)
    from_iso = from_dt.strftime("%Y-%m-%d %H:%M:%S")
    to_iso = to_dt.strftime("%Y-%m-%d %H:%M:%S")

    updates: list[tuple[str, float, str]] = []
    skipped = 0
    for r in rows:
        sym = r["symbol"]
        sec = r["security_id"]
        seg = r["exchange_segment"] or "BSE_EQ"
        if not sec:
            skipped += 1
            continue
        try:
            bars = await dhan.intraday(sec, seg, 1, from_iso, to_iso)
        except (DhanUnavailable, DhanError) as e:
            log.debug("ltp poll: skip %s (%s)", sym, e)
            skipped += 1
            continue
        if not bars:
            continue
        last = bars[-1]
        updates.append((sym, float(last.close), now.isoformat()))

    if not updates:
        return
    conn = connect()
    try:
        with tx(conn):
            for sym, ltp, ts in updates:
                conn.execute(
                    "INSERT INTO live_ltp (symbol, ltp, fetched_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(symbol) DO UPDATE SET ltp=excluded.ltp, fetched_at=excluded.fetched_at",
                    (sym, ltp, ts),
                )
    finally:
        conn.close()
    if skipped:
        log.debug("ltp poll: updated=%d skipped=%d", len(updates), skipped)


COMMAND_RUN_REBALANCE = "run_rebalance.now"
# Sentinel files older than this are treated as stale — likely the worker
# was down when the user clicked, and the intent is no longer current.
COMMAND_MAX_AGE_SECONDS = 300


async def command_inbox_job(
    dhan: DhanClient,
    provider: UniverseProvider | None = None,
) -> None:
    """Poll ``run/commands/`` for one-shot UI signals (FRD B.2).

    Currently handles a single command: ``run_rebalance.now``. Dropping this
    file is how the web process asks the worker to run the consolidated
    09:30 signal+execution job off-schedule (manual "run now" button).

    Semantics:
    - **Idempotency guard still applies (FRD B.13).** If
      ``sessions.execution_completed_at`` is set for today, the command is
      rejected with an alert and the file consumed. The user cannot force a
      second run once paper fills or live orders exist for the day.
    - **Safety gates still apply.** ``execution_job`` does its own Dhan
      market-status check; if the market is closed the rebalance is a no-op
      with an info-level alert, exactly like the scheduled 09:30 run.
    - **Stale commands rejected.** Files older than ``COMMAND_MAX_AGE_SECONDS``
      are deleted without running (worker was down when the user clicked;
      intent is no longer current).
    - Unknown command files are logged and removed.
    """
    inbox = command_inbox()
    if not inbox.exists():
        return
    for p in sorted(inbox.iterdir()):
        if not p.is_file() or not p.name.endswith(".now"):
            continue
        try:
            age_s = now_ist().timestamp() - p.stat().st_mtime
        except OSError:
            continue

        if p.name == COMMAND_RUN_REBALANCE:
            conn = connect()
            try:
                if age_s > COMMAND_MAX_AGE_SECONDS:
                    raise_alert(
                        conn,
                        Alert(
                            severity="warn",
                            source="commands",
                            message=f"run_rebalance command is stale ({int(age_s)}s old); discarded",
                        ),
                    )
                    _safe_unlink(p)
                    continue

                sess = session_date_for(now_ist())
                done = conn.execute(
                    "SELECT execution_completed_at FROM sessions WHERE session_date = ?",
                    (sess.isoformat(),),
                ).fetchone()
                if done and done["execution_completed_at"]:
                    raise_alert(
                        conn,
                        Alert(
                            severity="warn",
                            source="commands",
                            message=f"run_rebalance rejected: execution already completed for {sess.isoformat()}",
                        ),
                    )
                    _safe_unlink(p)
                    continue

                raise_alert(
                    conn,
                    Alert(
                        severity="info",
                        source="commands",
                        message=f"manual run_rebalance triggered for {sess.isoformat()}",
                    ),
                )
            finally:
                conn.close()

            # Consume the sentinel BEFORE calling execution_job. If the job
            # raises we still want the file gone so we don't loop-retry on
            # every 2-second tick.
            _safe_unlink(p)
            try:
                await execution_job(dhan, provider=provider)
            except Exception as e:  # noqa: BLE001
                conn = connect()
                try:
                    raise_alert(
                        conn,
                        Alert(
                            severity="error",
                            source="commands",
                            message=f"manual run_rebalance failed: {type(e).__name__}: {e}",
                        ),
                    )
                finally:
                    conn.close()
            continue

        # Unknown command — log, alert, clean up.
        conn = connect()
        try:
            raise_alert(
                conn,
                Alert(
                    severity="warn",
                    source="commands",
                    message=f"unknown command file ignored: {p.name}",
                ),
            )
        finally:
            conn.close()
        _safe_unlink(p)


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError as e:
        log.warning("failed to delete command file %s: %s", p, e)


async def execution_job(
    dhan: DhanClient,
    provider: UniverseProvider | None = None,
    capital_override: float | None = None,
    force: bool = False,
) -> None:
    """09:30 IST — single consolidated signal + execution job. FRD B.5.

    Steps (mirror FRD B.5 numbered list):
    1. Market-status check.
    2. Fetch daily OHLCV lookback, compute indicators and relative returns.
    3. Narrow to static-eligibility survivors; fetch 09:25-09:29 intraday
       candles for those symbols; compute vol_0925_0930 and apply the volume
       gate.
    4. Rank the volume-qualified set by relative_return_63d, take top 20,
       build target weights under the configured weight_scheme (inv_atr
       baseline: w_i proportional to 1 / atr_pct_i) and cash-aware target
       quantities.
    5. Persist `signals` rows and generate paper orders via diff against
       current paper_book.
    6. Fetch 09:30 minute-candle close; fill paper orders; if live enabled,
       place tagged Dhan orders and propagate actual fill qty back to paper
       (parity rule).
    7. Mark sessions.execution_completed_at to block re-runs (FRD B.13).

    ``force=True`` bypasses (1) the market-open check and (2) the idempotency
    guard. Intended for the post-market same-day backfill tool
    (``python -m app.tools.backfill_today``); never used by the scheduled run.
    """
    provider = provider or default_provider()
    conn = connect()
    try:
        if not force and not await _market_is_open(dhan, conn):
            return

        sess = session_date_for(now_ist())
        # Idempotency guard (FRD B.13): the consolidated job is non-idempotent
        # once live orders or paper fills have been written.
        done = conn.execute(
            "SELECT execution_completed_at FROM sessions WHERE session_date = ?", (sess.isoformat(),)
        ).fetchone()
        if not force and done and done["execution_completed_at"]:
            log.info("execution already completed for %s; skipping", sess)
            return

        universe = provider.load()
        if not universe:
            raise_alert(conn, Alert(severity="warn", source="jobs", message="empty universe; skip"))
            return

        # Step 2: daily indicators.
        from_date = (sess - timedelta(days=400)).isoformat()
        to_date = sess.isoformat()

        bars_by_sec: dict[str, list[OHLCBar]] = {}
        skipped_client = 0
        skipped_unavailable = 0
        for u in universe:
            try:
                bars = await dhan.historical_daily(u.security_id, u.exchange_segment, from_date, to_date)
            except DhanUnavailable as e:
                # Upstream hiccup — log and move on; a single symbol must not
                # take down a rebalance of ~2k symbols.
                skipped_unavailable += 1
                if skipped_unavailable <= 5:
                    log.warning("historical fetch transport-failed for %s: %s", u.symbol, e)
                continue
            except DhanError as e:
                # 4xx on a single symbol (bad security_id, delisted, segment
                # mismatch, etc.) must not crash the whole job. Log the first
                # few with payload so we can debug Dhan-side drift, then skip.
                skipped_client += 1
                if skipped_client <= 5:
                    log.warning(
                        "historical fetch rejected for %s (sec_id=%s seg=%s): %s payload=%s",
                        u.symbol, u.security_id, u.exchange_segment, e, getattr(e, "payload", None),
                    )
                continue
            if bars:
                bars_by_sec[u.security_id] = bars
        if skipped_client or skipped_unavailable:
            log.info(
                "historical fetch done: ok=%d client_err=%d transport_err=%d",
                len(bars_by_sec), skipped_client, skipped_unavailable,
            )

        panel = _bars_to_panel(bars_by_sec, universe)
        if panel.empty:
            raise_alert(conn, Alert(severity="warn", source="jobs", message="no historical data; skip"))
            return

        metrics = compute_universe_metrics(panel, DEFAULT_CONFIG)

        # Step 3: volume gate. Only fetch intraday candles for static-eligibility survivors.
        static_survivors = set(static_eligible_symbols(metrics, sess, DEFAULT_CONFIG))
        intraday_volumes = await _fetch_0925_0930_volumes(dhan, universe, sess, static_survivors)

        # Step 4: build target set (applies static filters + volume gate + ranking + sizing).
        capital = capital_override if capital_override is not None else _capital_for(conn)
        target = build_target_set(metrics, sess, capital=capital, intraday_volumes=intraday_volumes)

        # Step 5: persist signals and generate paper orders.
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
                "INSERT INTO sessions (session_date, market_open) VALUES (?, 1)"
                " ON CONFLICT(session_date) DO UPDATE SET market_open=1",
                (sess.isoformat(),),
            )
        paper_generate(conn, sess)

        # Debug artifact (FRD B.10).
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
                    "intraday_volumes": intraday_volumes,
                    "computed_at": now_ist().isoformat(),
                },
                f,
                indent=2,
            )

        # Step 6: fill paper + place live orders.
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

        prices = await _fetch_0930_closes(dhan, conn, sess, [t[1] for t in tuples])
        price_fetcher = lambda sym, _prices=prices: _prices.get(sym)
        paper_execute(conn, sess, price_fetcher, qty_override=overrides)

        compute_paper_pnl(conn, sess, ltp_fetcher=price_fetcher)

        # Step 7: mark completed.
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


async def _fetch_0925_0930_volumes(
    dhan: DhanClient,
    universe: list[UniverseEntry],
    sess: date,
    symbols: set[str],
) -> dict[str, float]:
    """Sum traded volume across the five one-minute candles starting at 09:25
    and ending before 09:30 (09:25, 09:26, 09:27, 09:28, 09:29) for each
    symbol in `symbols`. FRD A.3, A.5.

    Returns {symbol: volume}. Symbols with missing or failed candle fetches
    are left out, which fail-closes the volume gate in `_volume_ok`.
    """
    out: dict[str, float] = {}
    if not symbols:
        return out
    by_name = {u.symbol: u for u in universe}
    from_iso = f"{sess.isoformat()}T09:25:00"
    to_iso = f"{sess.isoformat()}T09:30:00"
    for sym in symbols:
        u = by_name.get(sym)
        if u is None:
            continue
        try:
            bars = await dhan.intraday(u.security_id, u.exchange_segment, 1, from_iso, to_iso)
        except DhanUnavailable as e:
            log.warning("intraday volume fetch failed for %s: %s", sym, e)
            continue
        except DhanError as e:
            # Fail-closed: treat a client-side rejection like a missing candle
            # so the volume gate rejects. Logging preserves debuggability.
            log.warning("intraday volume rejected for %s: %s payload=%s", sym, e, getattr(e, "payload", None))
            continue
        if not bars:
            continue
        out[sym] = float(sum(b.volume for b in bars))
    return out


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
        except DhanError as e:
            log.warning("intraday fetch rejected for %s: %s payload=%s", sym, e, getattr(e, "payload", None))
            out[sym] = None
            continue
        out[sym] = bars[0].close if bars else None
    return out
