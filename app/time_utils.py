"""IST time helpers. FRD B.5, B.13."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")

SIGNAL_TIME = time(9, 10)
EXECUTION_TIME = time(9, 30)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
EOD_CUTOVER = time(15, 35)


def now_ist() -> datetime:
    return datetime.now(IST)


def now_utc() -> datetime:
    return datetime.now(UTC)


def today_ist_date():
    return now_ist().date()


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; attach tz before converting")
    return dt.astimezone(IST)


def is_market_hours(dt: datetime | None = None) -> bool:
    """True when within NSE/BSE equity cash session hours.
    This is a wall-clock check only; actual trading-day status comes from
    Dhan market-status API per FRD B.5.
    """
    dt = (dt or now_ist()).astimezone(IST)
    if dt.weekday() >= 5:
        return False
    t = dt.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def session_date_for(dt: datetime | None = None):
    """The trading session date a given IST timestamp belongs to.

    FRD B.15: signal/exec for date D happens in the morning of date D.
    Before 09:10 on a weekday we treat as 'next session = today'. On weekends
    session_date is the last weekday.
    """
    dt = (dt or now_ist()).astimezone(IST)
    d = dt.date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d
