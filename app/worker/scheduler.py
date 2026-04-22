"""APScheduler wiring. FRD B.5."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.dhan.client import DhanClient
from app.time_utils import IST
from app.worker.jobs import (
    execution_job,
    recon_job,
    signal_job,
    token_expiry_monitor_job,
    token_watcher_job,
)

log = logging.getLogger("scheduler")


def build_scheduler(dhan: DhanClient) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)

    scheduler.add_job(
        signal_job, CronTrigger(day_of_week="mon-fri", hour=9, minute=10, timezone=IST),
        args=(dhan,), id="signal", misfire_grace_time=300, coalesce=True,
    )
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
    return scheduler
