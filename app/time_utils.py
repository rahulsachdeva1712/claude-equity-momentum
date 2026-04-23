"""IST time helpers. FRD B.5, B.13."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")

EXECUTION_TIME = time(9, 30)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
EOD_CUTOVER = time(15, 35)
# FRD A.3/A.5: intraday volume gate window — sum of the five one-minute candles
# starting at 09:25 up to but not including 09:30 (09:25, 09:26, 09:27, 09:28, 09:29).
VOLUME_WINDOW_START = time(9, 25)
VOLUME_WINDOW_END = time(9, 30)


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

    FRD B.15: signal+execution for date D happens at 09:30 IST on date D.
    On weekdays we treat any time-of-day as 'session = today'; on weekends
    session_date falls back to the last weekday.
    """
    dt = (dt or now_ist()).astimezone(IST)
    d = dt.date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d
