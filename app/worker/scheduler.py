"""APScheduler wiring. FRD B.5."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.dhan.client import DhanClient
from app.time_utils import IST
from app.worker.jobs import (
    command_inbox_job,
    execution_job,
    ltp_poll_job,
    market_status_poll_job,
    paper_mtm_refresh_job,
    recon_job,
    token_expiry_monitor_job,
    token_watcher_job,
    universe_refresh_job,
)
from app.universe.refresh import universe_csv_path

log = logging.getLogger("scheduler")


def build_scheduler(dhan: DhanClient) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)

    # FRD B.5: single consolidated signal + execution job at 09:30 IST. The
    # intraday volume gate (A.5) is a same-session measurement, so there is
    # no separate pre-market signal step.
    scheduler.add_job(
        execution_job, CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=IST),
        args=(dhan,), id="execution", misfire_grace_time=60, coalesce=True,
    )
    # Recon during market hours only. APScheduler 3.x doesn't have a window
    # combinator, so use a cron that fires every 15s within the window.
    scheduler.add_job(
        recon_job, CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*", second="*/15", timezone=IST),
        args=(dhan,), id="recon", misfire_grace_time=10, coalesce=True, max_instances=1,
    )

    tok_state: dict = {}
    scheduler.add_job(
        token_watcher_job, CronTrigger(second="*/10", timezone=IST),
        args=(dhan, tok_state), id="token_watcher", coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        token_expiry_monitor_job, CronTrigger(minute="*", timezone=IST),
        args=(dhan, tok_state), id="token_expiry", coalesce=True, max_instances=1,
    )
    # Poll Dhan market status every 30s and persist to the `settings` table.
    # The web process reads this for the top-bar pill (FRD B.2 forbids web
    # making Dhan calls, so the worker is the only writer). ~2 RPM is well
    # under Dhan's quota. Staleness is handled on the read side.
    scheduler.add_job(
        market_status_poll_job, CronTrigger(second="*/30", timezone=IST),
        args=(dhan,), id="market_status", coalesce=True, max_instances=1,
    )
    # Per-position LTP poll for the paper tab's Marked Price column. Runs
    # every 60s during market hours only (outside market hours, Dhan returns
    # stale candles and the UI's "stale" marker handles that). Written to
    # the `live_ltp` table; read by views.paper_book_rich.
    scheduler.add_job(
        ltp_poll_job, CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*", timezone=IST),
        args=(dhan,), id="ltp_poll", coalesce=True, max_instances=1,
    )
    # Paper MTM refresh: rewrite today's paper_pnl_daily row using live_ltp
    # so the headline KPI tiles and the Trade Log portfolio value reflect
    # the open book's mark-to-market, not just the morning fill snapshot.
    # Cron is once a minute Mon-Fri across the full day window so the row
    # also catches the daily close after market hours (the ltp_poll_job's
    # last 15:30 candle remains the freshest mark until the next session).
    scheduler.add_job(
        paper_mtm_refresh_job,
        CronTrigger(day_of_week="mon-fri", minute="*", timezone=IST),
        id="paper_mtm_refresh", coalesce=True, max_instances=1,
    )
    # Command inbox poll (FRD B.2). 2-second cadence; matches the doc's
    # "2-second cadence from worker" note for UI → worker signalling.
    scheduler.add_job(
        command_inbox_job, CronTrigger(second="*/2", timezone=IST),
        args=(dhan,), id="command_inbox", coalesce=True, max_instances=1,
    )
    # Daily universe refresh (FRD A.2, B.5). 18:00 IST Mon-Fri — well after
    # BSE's 15:30 close and the ~16:30 IST bhavcopy publish. Downloads the
    # BSE bhavcopy, applies the Champion B universe filter (series +
    # 20-day ADV >= 10k shares), joins to a cached Dhan scrip master on
    # ISIN, and writes <state_dir>/universe/universe.csv. This is the
    # artifact the CsvUniverseProvider reads at the 09:30 execution job.
    scheduler.add_job(
        universe_refresh_job, CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone=IST),
        id="universe_refresh", misfire_grace_time=3600, coalesce=True, max_instances=1,
    )
    # One-shot refresh on worker startup if the artifact is missing, so a
    # freshly installed app has something to trade with on day one rather
    # than silently running with an empty universe.
    if not universe_csv_path().exists():
        scheduler.add_job(
            universe_refresh_job,
            next_run_time=datetime.now(IST) + timedelta(seconds=5),
            id="universe_refresh_bootstrap", max_instances=1,
        )
    return scheduler
