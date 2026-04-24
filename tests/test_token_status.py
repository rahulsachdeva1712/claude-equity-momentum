"""Top-bar token classifier. Distinguishes missing, invalid, expiring, valid."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.time_utils import IST
from app.web.views import _classify_token


_TEST_KEY = "x" * 32  # length-only; signature is never verified by the classifier


def _jwt_at(exp_dt: datetime) -> str:
    return jwt.encode({"exp": int(exp_dt.timestamp())}, key=_TEST_KEY, algorithm="HS256")


# Anchor "now" at a weekday late evening IST so day-name boundaries are stable.
NOW_IST = datetime(2026, 4, 24, 9, 0, tzinfo=IST)  # Friday 09:00 IST
NOW = NOW_IST.astimezone(timezone.utc)


def test_missing_token_shows_no_token():
    state, label = _classify_token("")
    assert state == "missing"
    assert "no token" in label


def test_unparseable_token_shows_invalid_with_hint():
    state, label = _classify_token("not-a-jwt-at-all")
    assert state == "invalid"
    assert "BOM" in label or "quote" in label.lower()


def test_bom_prefixed_value_is_invalid():
    """If the BOM survived into the value (e.g. .env wasn't bom-stripped),
    classifier should still flag as invalid rather than crash."""
    state, _ = _classify_token("\ufeffeyJsomething.bad")
    assert state == "invalid"


def test_expired_today_shows_clock_and_just_now():
    exp = NOW_IST - timedelta(seconds=30)
    state, label = _classify_token(_jwt_at(exp), now=NOW)
    assert state == "expired"
    assert label == f"expired at {exp.strftime('%H:%M')} IST today (just now)"


def test_expired_today_shows_minutes_ago():
    exp = NOW_IST - timedelta(minutes=12)
    state, label = _classify_token(_jwt_at(exp), now=NOW)
    assert state == "expired"
    assert label == f"expired at {exp.strftime('%H:%M')} IST today (12m ago)"


def test_expired_yesterday_shows_h_m_ago():
    # Friday 09:00 IST minus 10h 12m -> Thursday 22:48 IST.
    exp = NOW_IST - timedelta(hours=10, minutes=12)
    state, label = _classify_token(_jwt_at(exp), now=NOW)
    assert state == "expired"
    assert label == f"expired at {exp.strftime('%H:%M')} IST yesterday (10h 12m ago)"


def test_expired_earlier_this_week_shows_weekday():
    # 3 days ago from Friday -> Tuesday.
    exp = NOW_IST - timedelta(days=3, hours=1)
    state, label = _classify_token(_jwt_at(exp), now=NOW)
    assert state == "expired"
    assert "Tuesday" in label
    assert "3d ago" in label


def test_expired_long_ago_shows_iso_date():
    exp = NOW_IST - timedelta(days=30)
    state, label = _classify_token(_jwt_at(exp), now=NOW)
    assert state == "expired"
    assert exp.date().isoformat() in label


def test_expiring_token_within_an_hour():
    exp = NOW_IST + timedelta(minutes=10)
    state, label = _classify_token(_jwt_at(exp), now=NOW)
    assert state == "expiring"
    assert label == f"expires in 10 min at {exp.strftime('%H:%M')} IST"


def test_valid_token_shows_clock_and_h_m():
    exp = NOW_IST + timedelta(hours=4, minutes=22)
    state, label = _classify_token(_jwt_at(exp), now=NOW)
    assert state == "valid"
    assert label == f"valid until {exp.strftime('%H:%M')} IST (4h 22m)"


@pytest.mark.parametrize(
    "delta,expected_suffix",
    [
        (timedelta(seconds=5), "(just now)"),
        (timedelta(minutes=1), "(1m ago)"),
        (timedelta(minutes=59), "(59m ago)"),
        (timedelta(hours=1, minutes=0), "(1h 0m ago)"),
        (timedelta(hours=23, minutes=59), "(23h 59m ago)"),
        (timedelta(days=2, hours=3), "(2d ago)"),
    ],
)
def test_expired_relative_time_format(delta, expected_suffix):
    exp = NOW_IST - delta
    state, label = _classify_token(_jwt_at(exp), now=NOW)
    assert state == "expired"
    assert label.endswith(expected_suffix), label
