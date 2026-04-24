"""One-shot: reconstruct today's paper rebalance post-market.

Use when the scheduled 09:30 IST ``execution_job`` didn't run (e.g. worker
was down, historical-fetch bug prior to the skip-on-4xx patch). Dhan still
serves today's daily + intraday candles after the close, so signal
generation, volume gating, and paper fills can all be recomputed exactly
as they would have been at 09:30.

Behavior:
- Calls ``execution_job(force=True)``: bypasses the market-open guard and
  the ``sessions.execution_completed_at`` idempotency guard.
- Uses today's IST session date (``session_date_for(now_ist())``).
- Runs only against the universe CSV that was in place at 09:30; if the
  artifact is missing the tool aborts (rather than silently trading an
  empty universe).
- Live-trading is **disabled** even if ``is_live_enabled(conn)`` is true,
  to prevent placing orders hours after the 09:30 slot.
- Single-writer invariant (FRD B.2): the worker must be stopped before
  this runs. The tool refuses to start if ``run/worker.pid`` is live.

Usage:
    # stop the worker first
    python -m app.tools.backfill_today
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app.db import connect, init_db
from app.dhan.client import DhanClient
from app.paths import log_file, state_dir
from app.pidfile import check_stale
from app.redaction import configure_logging
from app.settings import load_settings
from app.time_utils import now_ist
from app.universe.refresh import universe_csv_path
from app.worker.jobs import execution_job

log = logging.getLogger("backfill_today")


def _worker_live_pid() -> int | None:
    """Return the worker PID if it's still a live process, else None.

    Reuses ``app.pidfile.check_stale`` so the liveness semantics exactly
    match the worker's own PidFile layer (JSON format + psutil cmdline
    check that guards against recycled PIDs).
    """
    info = check_stale("worker")
    if info.reason == "live process":
        return info.previous_pid
    return None


async def _run() -> int:
    settings = load_settings()

    running = _worker_live_pid()
    if running is not None:
        log.error(
            "worker is running (pid=%s). Stop it before running backfill_today "
            "to preserve the single-writer invariant (FRD B.2).",
            running,
        )
        return 2

    if not universe_csv_path().exists():
        log.error(
            "universe artifact missing at %s — nothing to trade. Run the "
            "nightly universe_refresh first.",
            universe_csv_path(),
        )
        return 3

    # Prevent any accidental live-order placement: the execution_job reads
    # this flag from settings table. We don't mutate it; instead we rely on
    # the operator to have kept live off post-market. If live IS on, abort.
    init_db()
    conn = connect()
    try:
        from app.live.engine import is_live_enabled  # local import to avoid cycles
        if is_live_enabled(conn):
            log.error(
                "live trading is enabled. Refusing to backfill post-market "
                "since that would place live orders far outside the 09:30 slot. "
                "Disable live first (/actions/live-off), then re-run."
            )
            return 4
    finally:
        conn.close()

    dhan = DhanClient(
        base_url=settings.dhan_api_base,
        client_id=settings.dhan_client_id,
        access_token=settings.dhan_access_token,
    )
    try:
        log.info(
            "starting post-market backfill for %s (state_dir=%s)",
            now_ist().date().isoformat(),
            state_dir(),
        )
        await execution_job(dhan, force=True)
        log.info("backfill finished. Inspect the paper tab to verify.")
        return 0
    finally:
        await dhan.close()


def main() -> int:
    settings = load_settings()
    configure_logging(log_path=log_file("backfill_today"), level=settings.log_level)
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
