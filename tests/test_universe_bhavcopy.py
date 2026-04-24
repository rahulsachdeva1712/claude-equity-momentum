"""Bhavcopy download + parse. Verifies the Champion B universe pipeline's
front door can handle both BSE on-wire formats and is resilient to the
common failure modes (weekend, 404, HTML-as-CSV).
"""
from __future__ import annotations

import datetime as dt
import io
import zipfile

import pandas as pd
import pytest

from app.universe.bhavcopy import (
    BHAV_URL_NEW,
    BHAV_URL_OLD,
    fetch_bhavcopy,
    load_recent_bhavcopies,
    parse_bhavcopy,
)


# Minimal but schema-faithful sample of the new-format BSE bhavcopy. Two
# equity rows (A-group + T-group) + one non-equity row that must be filtered.
_NEW_CSV = (
    "TradDt,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
    "OpnPric,HghPric,LwPric,ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal\n"
    "2026-04-22,STK,500325,INE002A01018,RELIANCE,A,2900.0,2950.0,2880.0,2930.0,2895.0,1500000,4394500000\n"
    "2026-04-22,STK,500010,INE004A01022,HDFC,T,1700.0,1710.0,1680.0,1690.0,1701.0,8000,13570000\n"
    # Non-equity row (e.g. ETF) — should be dropped.
    "2026-04-22,ETF,100001,INF204K01K32,NIFTYBEES,EQ,220.0,221.0,219.0,220.5,220.0,50000,11025000\n"
).encode()


# Minimal legacy-format bhavcopy CSV (zipped). SC_TYPE='Q' = equity.
def _old_zip_bytes() -> bytes:
    csv_text = (
        "SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,ISIN_CODE,OPEN,HIGH,LOW,CLOSE,"
        "PREVCLOSE,NO_OF_SHRS,NET_TURNOV,TRADING_DATE\n"
        "500325,RELIANCE,A,Q,INE002A01018,2900.00,2950.00,2880.00,2930.00,"
        "2895.00,1500000,4394500000,22-APR-26\n"
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("EQ_ISINCODE_220426.CSV", csv_text)
    return buf.getvalue()


def test_parse_new_format_drops_non_equity(tmp_path):
    p = tmp_path / "bhav_20260422.csv"
    p.write_bytes(_NEW_CSV)
    df = parse_bhavcopy(p)
    assert len(df) == 2
    assert set(df["symbol"]) == {"RELIANCE", "HDFC"}
    assert set(df["series"]) == {"A", "T"}
    # Numerics coerced.
    assert df["close"].dtype.kind == "f"
    assert df.loc[df["symbol"] == "RELIANCE", "volume"].iloc[0] == 1_500_000
    # Date parsed.
    assert pd.Timestamp(df["date"].iloc[0]).date() == dt.date(2026, 4, 22)


def test_parse_old_format_zip_drops_non_q(tmp_path):
    p = tmp_path / "bhav_20220422.zip"
    p.write_bytes(_old_zip_bytes())
    df = parse_bhavcopy(p)
    assert len(df) == 1
    assert df["symbol"].iloc[0] == "RELIANCE"
    assert pd.Timestamp(df["date"].iloc[0]).date() == dt.date(2026, 4, 22)


def test_parse_unknown_extension_raises(tmp_path):
    p = tmp_path / "bhav_20260422.txt"
    p.write_text("whatever")
    with pytest.raises(ValueError):
        parse_bhavcopy(p)


def test_parse_new_format_rejects_random_csv(tmp_path):
    p = tmp_path / "bogus.csv"
    p.write_bytes(b"foo,bar\n1,2\n")
    with pytest.raises(ValueError):
        parse_bhavcopy(p)


def test_fetch_skips_weekend(tmp_path):
    # Sunday — should not attempt any HTTP.
    calls: list[str] = []

    def spy(url: str) -> bytes | None:
        calls.append(url)
        return b"x" * 10_000

    out = fetch_bhavcopy(dt.date(2026, 4, 26), tmp_path, http_get=spy)  # Sunday
    assert out is None
    assert calls == []


def test_fetch_uses_cached_new_format(tmp_path):
    day = dt.date(2026, 4, 22)  # Wed
    existing = tmp_path / f"bhav_{day.strftime('%Y%m%d')}.csv"
    existing.write_bytes(_NEW_CSV)

    def should_not_be_called(url: str):
        raise AssertionError(f"http_get called with {url} despite cache")

    out = fetch_bhavcopy(day, tmp_path, http_get=should_not_be_called)
    assert out == existing


def test_fetch_downloads_new_format(tmp_path):
    day = dt.date(2026, 4, 22)
    calls: list[str] = []

    def fake_get(url: str) -> bytes | None:
        calls.append(url)
        if "BhavCopy_BSE_CM" in url:
            return _NEW_CSV * 3  # pad to > MIN_NEW_BYTES
        return None

    out = fetch_bhavcopy(day, tmp_path, http_get=fake_get)
    assert out is not None
    assert out.suffix == ".csv"
    assert out.exists()
    assert len(calls) == 1  # new-format URL, no fallback needed
    assert BHAV_URL_NEW.format(ymd="20260422") in calls


def test_fetch_falls_back_to_old_format_on_404(tmp_path):
    day = dt.date(2022, 1, 3)
    zip_data = _old_zip_bytes()
    calls: list[str] = []

    def fake_get(url: str) -> bytes | None:
        calls.append(url)
        if "BhavCopy_BSE_CM" in url:
            return None  # 404
        return zip_data

    out = fetch_bhavcopy(day, tmp_path, http_get=fake_get)
    assert out is not None
    assert out.suffix == ".zip"
    assert len(calls) == 2
    assert BHAV_URL_OLD.format(dmy="030122") in calls


def test_fetch_rejects_html_masquerading_as_csv(tmp_path):
    day = dt.date(2026, 4, 22)

    def fake_get(url: str) -> bytes | None:
        # BSE sometimes returns an HTML error page with 200 OK.
        return b"<!DOCTYPE html><html><body>oops</body></html>" + b"x" * 10_000

    out = fetch_bhavcopy(day, tmp_path, http_get=fake_get)
    assert out is None


def test_load_recent_bhavcopies_concats_and_handles_gaps(tmp_path):
    # Three weekdays of data, one weekday 404, one weekend.
    frames: list[str] = []

    def fake_get(url: str) -> bytes | None:
        # Only return data for specific dates.
        for ymd in ("20260420", "20260421", "20260422"):
            if ymd in url:
                return _NEW_CSV
        return None

    df = load_recent_bhavcopies(
        tmp_path, end=dt.date(2026, 4, 23), lookback_days=7, http_get=fake_get
    )
    assert not df.empty
    # Three trading days, 2 rows each -> 6 total.
    assert len(df) == 6
    assert set(df["symbol"]) == {"RELIANCE", "HDFC"}
