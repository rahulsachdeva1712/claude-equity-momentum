"""Pre-market health check. Read-only inspection of state DB + filesystem
artifacts. Intended to be run from the repo root before market open."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from app.db import connect
from app.paper.engine import paper_cash, paper_portfolio_value
from app.paths import command_inbox, pid_file, state_dir
from app.universe.refresh import universe_csv_path
from app.settings import load_settings
from app.web.views import _classify_token as classify_token
from app.time_utils import IST, is_market_hours, now_ist, session_date_for

OK = "[OK]   "
WARN = "[WARN] "
ERR = "[ERR]  "


def line(level: str, msg: str) -> None:
    print(f"{level}{msg}")


now = now_ist()
today = now.date()
sess_for_today = session_date_for(now)

print("=" * 60)
print(f"Health check at {now.strftime('%Y-%m-%d %H:%M:%S')} IST")
print(f"  today (IST):              {today}")
print(f"  session_date_for(now):    {sess_for_today}")
print(f"  is_market_hours():        {is_market_hours()}")
print(f"  weekday:                  {now.strftime('%A')}")
print("=" * 60)
print()

conn = connect()
conn.row_factory = sqlite3.Row

# 1. Stale PENDING orders
print("# 1. Stale orders")
stale = conn.execute(
    "SELECT session_date, symbol, action, order_qty FROM paper_orders"
    " WHERE status = 'PENDING' AND session_date < ?",
    (sess_for_today.isoformat(),),
).fetchall()
if stale:
    line(ERR, f"{len(stale)} PENDING paper orders for prior sessions:")
    for r in stale:
        print(f"        {dict(r)}")
else:
    line(OK, "no stale PENDING paper orders")

try:
    stale_live = conn.execute(
        "SELECT session_date, symbol FROM live_orders"
        " WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','SKIPPED') AND session_date < ?",
        (sess_for_today.isoformat(),),
    ).fetchall()
    if stale_live:
        line(ERR, f"{len(stale_live)} non-terminal live orders for prior sessions:")
        for r in stale_live:
            print(f"        {dict(r)}")
    else:
        line(OK, "no stale live orders")
except sqlite3.OperationalError as e:
    line(WARN, f"live_orders check skipped: {e}")
print()

# 2. Command inbox sentinels
print("# 2. Command inbox")
inbox = command_inbox()
if inbox.exists():
    files = list(inbox.iterdir())
    if not files:
        line(OK, "command inbox clean")
    else:
        for p in files:
            age = now.timestamp() - p.stat().st_mtime
            level = WARN if age > 300 else OK
            line(level, f"  {p.name}  age={age:.0f}s")
else:
    line(OK, "no command inbox dir yet (fine)")
print()

# 3. Sessions table
print("# 3. Sessions")
last_sessions = conn.execute(
    "SELECT session_date, execution_completed_at FROM sessions ORDER BY session_date DESC LIMIT 5"
).fetchall()
for r in last_sessions:
    print(f"        session_date={r['session_date']}  execution_completed_at={r['execution_completed_at']}")
todays = conn.execute(
    "SELECT execution_completed_at FROM sessions WHERE session_date = ?",
    (sess_for_today.isoformat(),),
).fetchone()
if todays:
    if todays["execution_completed_at"]:
        line(OK, f"today's session ({sess_for_today}) already completed — single-run guard active")
    else:
        line(WARN, f"today's session row exists but execution_completed_at is null — incomplete prior run")
else:
    line(OK, f"no row for today ({sess_for_today}) — 09:30 will create one")
print()

# 4. Worker liveness
print("# 4. Worker liveness")
pid_path = pid_file("worker")
if pid_path.exists():
    try:
        info = json.loads(pid_path.read_text())
        pid = info.get("pid")
        started_at = info.get("started_at")
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        alive = str(pid) in out.stdout
        level = OK if alive else ERR
        line(level, f"worker pid={pid} started={started_at} alive={alive}")
    except Exception as e:
        line(WARN, f"worker pid file present but unreadable: {e}")
else:
    line(WARN, "no worker pid file — worker not running")
print()

# 5. Token state
print("# 5. Dhan token")
s = load_settings()
if not s.dhan_access_token:
    line(ERR, "DHAN_ACCESS_TOKEN missing")
else:
    state, label = classify_token(s.dhan_access_token, now=now)
    level = OK if state == "valid" else (WARN if state == "expiring" else ERR)
    line(level, f"state={state}  label={label}")
print()

# 6. Universe artifact
print("# 6. Universe artifact")
upath = universe_csv_path()
if upath.exists():
    mtime = datetime.fromtimestamp(upath.stat().st_mtime, tz=IST)
    age_hours = (now - mtime).total_seconds() / 3600
    n_row = conn.execute("SELECT value FROM settings WHERE key='universe_count'").fetchone()
    src_row = conn.execute("SELECT value FROM settings WHERE key='universe_source_date'").fetchone()
    ref_row = conn.execute("SELECT value FROM settings WHERE key='universe_refresh_at'").fetchone()
    n = n_row["value"] if n_row else "?"
    src = src_row["value"] if src_row else "?"
    ref = ref_row["value"] if ref_row else "?"
    level = OK if age_hours < 24 else (WARN if age_hours < 72 else ERR)
    line(level, f"universe.csv age={age_hours:.1f}h  count={n}  source_date={src}  refreshed_at={ref}")
else:
    line(ERR, f"universe.csv missing at {upath}")
print()

# 7. Paper book consistency
print("# 7. Paper book")
cash = paper_cash(conn)
pv = paper_portfolio_value(conn)
positions = conn.execute(
    "SELECT symbol, qty, avg_cost, cost_basis FROM paper_book ORDER BY symbol"
).fetchall()
total_cost = sum(float(r["cost_basis"]) for r in positions)
seed_row = conn.execute("SELECT value FROM settings WHERE key='paper_initial_capital'").fetchone()
seed = float(seed_row["value"]) if seed_row else 100_000.0
print(f"        seed:            Rs {seed:,.2f}")
print(f"        cash:            Rs {cash:,.2f}")
print(f"        cost basis sum:  Rs {total_cost:,.2f}")
print(f"        portfolio_value: Rs {pv:,.2f}")
print(f"        open positions:  {len(positions)}")
for r in positions:
    print(f"          {r['symbol']:12} qty={r['qty']:>5} avg={r['avg_cost']:.2f} cost={r['cost_basis']:.2f}")

if cash < -1.0:
    line(ERR, f"NEGATIVE cash {cash:.2f} — book is leveraged")
elif cash < 0.0:
    line(OK, f"cash slightly negative ({cash:.2f}) within rounding tolerance")
else:
    line(OK, "cash non-negative")
if total_cost > seed * 1.05:
    line(ERR, f"cost basis exceeds seed by >5% — leverage")
else:
    line(OK, "cost basis within seed bound")
print()

# 8. paper_pnl_daily freshness
print("# 8. paper_pnl_daily")
last_pnl = conn.execute(
    "SELECT session_date, realized, unrealized, mtm, computed_at FROM paper_pnl_daily ORDER BY session_date DESC LIMIT 1"
).fetchone()
if last_pnl:
    try:
        ts = datetime.fromisoformat(last_pnl["computed_at"])
        age_min = (now - ts).total_seconds() / 60
        print(f"        latest session={last_pnl['session_date']}  computed_at={last_pnl['computed_at']}  age={age_min:.1f}m")
    except Exception:
        print(f"        latest: {dict(last_pnl)}")
    if last_pnl["session_date"] != sess_for_today.isoformat() and not is_market_hours():
        line(OK, "no row for today yet — expected pre-09:30")
else:
    line(WARN, "no paper_pnl_daily rows")
print()

# 9. Live LTP freshness
print("# 9. live_ltp")
ltps = conn.execute("SELECT symbol, ltp, fetched_at FROM live_ltp ORDER BY symbol").fetchall()
if not ltps:
    line(OK, "live_ltp empty (expected outside market hours)")
else:
    held = {r["symbol"] for r in positions}
    for r in ltps:
        try:
            age_h = (now - datetime.fromisoformat(r["fetched_at"])).total_seconds() / 3600
            tag = "(held)" if r["symbol"] in held else "(stale, not held)"
            print(f"        {r['symbol']:12} ltp={r['ltp']:.2f}  age={age_h:.1f}h  {tag}")
        except Exception:
            print(f"        {dict(r)}")
print()

# 10. Recent unacked alerts
print("# 10. Unacked alerts")
try:
    alerts = conn.execute(
        "SELECT severity, source, message, created_at FROM alerts WHERE acked_at IS NULL ORDER BY id DESC LIMIT 10"
    ).fetchall()
except sqlite3.OperationalError:
    alerts = conn.execute(
        "SELECT severity, source, message, created_at FROM alerts ORDER BY id DESC LIMIT 10"
    ).fetchall()
if not alerts:
    line(OK, "no unacked alerts")
else:
    line(WARN, f"{len(alerts)} alerts (latest 10):")
    for r in alerts:
        print(f"        [{r['severity']}] {r['source']}: {r['message']}  ({r['created_at']})")
print()

# 11. Live switch
print("# 11. Live trading")
le = conn.execute("SELECT value FROM settings WHERE key='live_enabled'").fetchone()
val = le["value"] if le else "0"
line(OK, f"live_enabled = {val}  ({'ON' if val == '1' else 'OFF'})")
print()

conn.close()
print("=" * 60)
print("Health check complete.")
