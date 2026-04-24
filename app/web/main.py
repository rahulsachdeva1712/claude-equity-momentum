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
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.alerts import acknowledge
from app.db import connect, init_db
from app.live.engine import is_live_enabled, set_live_enabled
from app.paths import env_file, log_file, pid_file
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
                    "summary": views.paper_summary(conn),
                    "signals": views.signals_for(conn, sess),
                    "book": views.paper_book_rows(conn),
                    "pnl_series": views.pnl_timeseries(conn, "paper"),
                    "fills": views.recent_fills(conn, "paper"),
                }
            )
            return templates.TemplateResponse(request, "paper.html", ctx)
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
