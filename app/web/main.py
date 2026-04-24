"""FastAPI web app entry. FRD B.2, B.9.

Two tabs (Paper / Live) plus a top bar with a settings modal (kill switch,
token status, worker status, alerts). Read-only view on SQLite plus a
handful of write endpoints for settings. Auto-refresh via HTMX.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import csv
import io

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.alerts import acknowledge
from app.db import connect, init_db
from app.live.engine import is_live_enabled, set_live_enabled
from app.paths import command_inbox, env_file, log_file, pid_file
from app.pidfile import AlreadyRunning, PidFile, check_stale
from app.redaction import configure_logging
from app.settings import load_settings
from app.time_utils import session_date_for, now_ist
from app.web import views

log = logging.getLogger("web")
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _worker_alive() -> bool:
    info = check_stale("worker")
    return info.reason == "live process"


def _context(request: Request) -> dict:
    settings = load_settings()
    conn = connect(readonly=False)
    try:
        live_on = is_live_enabled(conn)
        bar = views.top_bar_status(
            conn,
            token=settings.dhan_access_token,
            worker_pid_alive=_worker_alive(),
            live_enabled=live_on,
        )
        return {
            "request": request,
            "bar": bar,
            "alerts": views.alerts_unacked(conn, limit=20),
            "now": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "env_path": str(env_file()),
        }
    finally:
        conn.close()


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        yield

    app = FastAPI(title="Equity Momentum Rebalance", lifespan=lifespan)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return RedirectResponse(url="/paper")

    @app.get("/paper", response_class=HTMLResponse)
    async def paper(request: Request):
        conn = connect()
        try:
            ctx = _context(request)
            sess = session_date_for(now_ist())
            ctx.update(
                {
                    "summary": views.paper_summary_rich(conn),
                    "signals": views.signals_for(conn, sess),
                    "signals_brief": views.signals_today_brief(conn, sess),
                    "book": views.paper_book_rich(conn),
                    "pnl_series": views.pnl_timeseries(conn, "paper"),
                    "fills": views.recent_fills(conn, "paper"),
                    "meta": views.paper_meta(conn, sess),
                    "today": views.today_status(conn, sess),
                    "perf": views.performance_summary(conn),
                    "trade_log": views.day_grouped_trade_log(conn),
                }
            )
            return templates.TemplateResponse(request, "paper.html", ctx)
        finally:
            conn.close()

    @app.get("/paper/export/trade-log.csv")
    async def export_trade_log():
        """Export the full paper trade log as CSV (all sessions)."""
        conn = connect()
        try:
            groups = views.day_grouped_trade_log(conn, limit_days=10_000)
        finally:
            conn.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "session_date", "symbol", "side", "kind", "entry_at", "exit_at",
            "fill_price", "order_qty", "fill_qty", "profit_loss", "returns_pct",
            "non_broker_charges", "portfolio_value",
        ])
        for g in groups:
            for r in g["rows"]:
                w.writerow([
                    g["session_date"], r["symbol"], r["side"], r["kind"],
                    r["entry_at"], r["exit_at"], r["fill_price"], r["order_qty"],
                    r["fill_qty"], r["profit_loss"] if r["profit_loss"] is not None else "",
                    r["returns_pct"] if r["returns_pct"] is not None else "",
                    r["non_broker_charges"] if r["non_broker_charges"] is not None else "",
                    g["portfolio_value"],
                ])
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=paper-trade-log.csv"})

    @app.get("/config", response_class=HTMLResponse)
    async def config_tab(request: Request):
        """Read-only view of the Champion B strategy config + state paths."""
        from app import paths as _paths
        conn = connect()
        try:
            ctx = _context(request)
            ctx.update(
                {
                    "state_dir": str(_paths.state_dir()),
                    "db_path": str(_paths.db_file()),
                    "universe_csv": str(_paths.state_dir() / "universe" / "universe.csv"),
                }
            )
            return templates.TemplateResponse(request, "config.html", ctx)
        finally:
            conn.close()

    @app.get("/live", response_class=HTMLResponse)
    async def live(request: Request):
        conn = connect()
        try:
            ctx = _context(request)
            ctx.update(
                {
                    "summary": views.live_summary(conn),
                    "positions": views.live_positions(conn),
                    "pnl_series": views.pnl_timeseries(conn, "live"),
                    "fills": views.recent_fills(conn, "live"),
                    "live_on": ctx["bar"]["live_enabled"],
                }
            )
            return templates.TemplateResponse(request, "live.html", ctx)
        finally:
            conn.close()

    @app.get("/partials/top-bar", response_class=HTMLResponse)
    async def top_bar(request: Request):
        ctx = _context(request)
        return templates.TemplateResponse(request, "partials/top_bar.html", ctx)

    @app.post("/settings/live-enabled")
    async def toggle_live(enabled: str = Form(...)):
        conn = connect()
        try:
            set_live_enabled(conn, enabled == "1")
        finally:
            conn.close()
        return RedirectResponse(url="/paper", status_code=303)

    @app.post("/actions/run-rebalance")
    async def run_rebalance(request: Request):
        """Drop a sentinel in run/commands/ so the worker's command_inbox
        job picks it up within ~2s. FRD B.2 / B.13. We do NOT call the
        execution path from the web process (single-writer rule).

        The redirect target is the referrer when available so the user
        stays on whichever tab they clicked from.
        """
        inbox = command_inbox()
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "run_rebalance.now").touch()
        referer = request.headers.get("referer")
        return RedirectResponse(url=referer or "/paper", status_code=303)

    @app.post("/alerts/{alert_id}/ack")
    async def ack(alert_id: int):
        conn = connect()
        try:
            acknowledge(conn, alert_id)
        finally:
            conn.close()
        return RedirectResponse(url="/paper", status_code=303)

    return app


def main() -> int:
    settings = load_settings()
    configure_logging(log_path=log_file("web"), level=settings.log_level)
    try:
        with PidFile("web"):
            uvicorn.run(
                "app.web.main:create_app",
                host=settings.web_host,
                port=settings.web_port,
                factory=True,
                log_level=settings.log_level.lower(),
            )
        return 0
    except AlreadyRunning as e:
        log.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
