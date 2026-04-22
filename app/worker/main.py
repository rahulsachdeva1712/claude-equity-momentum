"""Worker daemon entry point. FRD B.2, B.10.

Run as:
    emrb-worker           (console-script installed by pyproject)
    python -m app.worker.main

Startup sequence:
1. Load settings + configure logging (redaction filter active).
2. Acquire PID file with stale cleanup.
3. Initialize SQLite schema (idempotent).
4. Instantiate Dhan client; run a self-check (FRD B.10).
5. Start the APScheduler event loop.

Shutdown: SIGTERM/SIGINT/atexit route to pidfile.release(), which runs
the registered shutdown callbacks (stop scheduler, close Dhan client,
close DB connections).
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app.alerts import Alert, raise_alert
from app.db import connect, init_db
from app.dhan.client import DhanClient
from app.paths import log_file
from app.pidfile import AlreadyRunning, PidFile
from app.redaction import configure_logging
from app.settings import load_settings
from app.worker.scheduler import build_scheduler

log = logging.getLogger("worker")


async def _self_check(dhan: DhanClient) -> None:
    """Post-startup smoke test (FRD B.10): token + market status + db."""
    conn = connect()
    try:
        ok = await dhan.validate_token()
        if not ok:
            raise_alert(conn, Alert(severity="error", source="worker", message="dhan token invalid at startup"))
        try:
            _ = await dhan.market_status()
        except Exception as e:  # noqa: BLE001
            raise_alert(conn, Alert(severity="warn", source="worker", message=f"market_status self-check failed: {e}"))
    finally:
        conn.close()


async def _run(pf: PidFile) -> int:
    settings = load_settings()
    dhan = DhanClient(
        base_url=settings.dhan_api_base,
        client_id=settings.dhan_client_id,
        access_token=settings.dhan_access_token,
    )

    scheduler = build_scheduler(dhan)

    def _shutdown_sync() -> None:
        try:
            if scheduler.running:
                scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass

    pf.register_shutdown(_shutdown_sync)
    pf.register_shutdown(lambda: asyncio.get_event_loop().run_until_complete(dhan.close()))

    init_db()
    scheduler.start()
    await _self_check(dhan)

    log.info("worker running. jobs: %s", [j.id for j in scheduler.get_jobs()])
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        return 0


def main() -> int:
    settings = load_settings()
    configure_logging(log_path=log_file("worker"), level=settings.log_level)

    try:
        with PidFile("worker") as pf:
            if pf.stale_info and pf.stale_info.cleaned:
                # Emit a deferred alert so the UI shows this on first load.
                conn = connect()
                try:
                    raise_alert(
                        conn,
                        Alert(
                            severity="warn",
                            source="worker",
                            message=f"cleaned stale pid file: {pf.stale_info.previous_pid} ({pf.stale_info.reason})",
                        ),
                    )
                finally:
                    conn.close()
            return asyncio.run(_run(pf))
    except AlreadyRunning as e:
        log.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
