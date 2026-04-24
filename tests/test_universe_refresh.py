"""Universe refresh job: end-to-end filter + ADV + scrip-join + CSV write.

Exercises the Champion B universe rules (series in {A,B,T,X,XT} + 20-day
ADV >= 10_000 shares, no turnover floor, no market-cap gate). Also verifies
the provider reads what the refresh writes.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.db import connect, init_db
from app.strategy.universe import CsvUniverseProvider
from app.universe.refresh import (
    SETTINGS_KEY_UNIVERSE_COUNT,
    SETTINGS_KEY_UNIVERSE_REFRESH_AT,
    SETTINGS_KEY_UNIVERSE_SOURCE_DATE,
    compute_universe_frame,
    join_scrip_master,
    refresh_universe,
    universe_csv_path,
    write_universe_csv,
)


# --------------------------------------------------------------------------
# Unit-level: filter + join without hitting the filesystem or network.
# --------------------------------------------------------------------------


def _panel(rows):
    cols = ["date", "sc_code", "isin", "symbol", "series",
            "open", "high", "low", "close", "prev_close", "volume", "turnover"]
    df = pd.DataFrame(rows, columns=cols)
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "prev_close", "volume", "turnover"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def test_compute_universe_frame_filters_series():
    # Two symbols. RELIANCE is A-group (keep). MF is a mutual-fund row in
    # series "F" (drop — not in {A,B,T,X,XT}).
    rows = []
    for i in range(22):
        d = dt.date(2026, 4, 1) + dt.timedelta(days=i)
        rows.append([d, "500325", "INE002A01018", "RELIANCE", "A",
                     100.0, 102.0, 99.0, 101.0, 100.0, 20_000, 2_020_000])
        rows.append([d, "100001", "INF204K01K32", "MFUND", "F",
                     50.0, 51.0, 49.5, 50.5, 50.0, 50_000, 2_525_000])
    df = _panel(rows)
    out = compute_universe_frame(df, as_of=dt.date(2026, 4, 22))
    assert list(out["symbol"]) == ["RELIANCE"]
    assert out.iloc[0]["adv_20d"] >= 10_000


def test_compute_universe_frame_enforces_adv_min():
    # Both A-group, but LOWVOL averages below 10k shares over the window.
    rows = []
    for i in range(22):
        d = dt.date(2026, 4, 1) + dt.timedelta(days=i)
        rows.append([d, "500325", "INE002A01018", "HIVOL", "A",
                     100.0, 102.0, 99.0, 101.0, 100.0, 50_000, 5_050_000])
        rows.append([d, "500010", "INE004A01022", "LOWVOL", "B",
                     50.0, 51.0, 49.5, 50.5, 50.0, 500, 25_250])
    df = _panel(rows)
    out = compute_universe_frame(df, as_of=dt.date(2026, 4, 22))
    assert list(out["symbol"]) == ["HIVOL"]


def test_compute_universe_frame_uses_fallback_date_when_anchor_missing():
    # Anchor date of 2026-05-01 doesn't exist in the frame; the function
    # should fall back to the latest available date (2026-04-22).
    rows = []
    for i in range(22):
        d = dt.date(2026, 4, 1) + dt.timedelta(days=i)
        rows.append([d, "500325", "INE002A01018", "RELIANCE", "A",
                     100.0, 102.0, 99.0, 101.0, 100.0, 20_000, 2_020_000])
    df = _panel(rows)
    out = compute_universe_frame(df, as_of=dt.date(2026, 5, 1))
    assert not out.empty


def test_join_scrip_master_drops_symbols_missing_from_scrip():
    uni = pd.DataFrame([
        {"sc_code": "500325", "isin": "INE002A01018", "symbol": "RELIANCE", "series": "A", "adv_20d": 20_000.0},
        {"sc_code": "500010", "isin": "", "symbol": "UNKNOWN", "series": "B", "adv_20d": 15_000.0},
    ])
    scrip = pd.DataFrame([
        {"security_id": "500325", "exchange_segment": "BSE_EQ", "trading_symbol": "RELIANCE"},
    ])
    out = join_scrip_master(uni, scrip)
    assert list(out["symbol"]) == ["RELIANCE"]
    assert out.iloc[0]["security_id"] == "500325"
    assert out.iloc[0]["exchange_segment"] == "BSE_EQ"


def test_join_emits_expected_columns():
    uni = pd.DataFrame([{
        "sc_code": "500325", "isin": "INE002A01018", "symbol": "RELIANCE",
        "series": "A", "adv_20d": 20_000.0,
    }])
    scrip = pd.DataFrame([{
        "security_id": "500325", "exchange_segment": "BSE_EQ", "trading_symbol": "RELIANCE",
    }])
    out = join_scrip_master(uni, scrip)
    assert list(out.columns) == [
        "symbol", "security_id", "exchange_segment", "market_cap_cr",
        "isin", "sc_code", "series", "adv_20d",
    ]
    # market_cap_cr is intentionally blank (Champion B has mcap filter off).
    assert out.iloc[0]["market_cap_cr"] == ""
    # ISIN is passed through from bhavcopy even though Dhan's scrip doesn't carry it.
    assert out.iloc[0]["isin"] == "INE002A01018"


def test_write_universe_csv_is_atomic(tmp_path):
    df = pd.DataFrame([{
        "symbol": "RELIANCE", "security_id": "500325", "exchange_segment": "BSE_EQ",
        "market_cap_cr": "", "isin": "INE002A01018", "sc_code": "500325",
        "series": "A", "adv_20d": 20000,
    }])
    path = tmp_path / "universe.csv"
    write_universe_csv(df, path)
    assert path.exists()
    text = path.read_text()
    assert text.startswith("symbol,security_id,")
    assert "RELIANCE" in text


# --------------------------------------------------------------------------
# Integration: refresh_universe end-to-end with injected HTTP fakes.
# --------------------------------------------------------------------------


_BHAV_NEW_TEMPLATE = (
    "TradDt,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
    "OpnPric,HghPric,LwPric,ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal\n"
    "{date},STK,500325,INE002A01018,RELIANCE,A,{o},{h},{lo},{c},{pc},{v},{tv}\n"
    "{date},STK,500010,INE004A01022,ONLYBS,B,{o},{h},{lo},{c},{pc},100,10000\n"  # too thin
)

_SCRIP_CSV = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
    "SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_SERIES\n"
    "BSE,E,500325,EQUITY,0,RELIANCE,1,A\n"
    "BSE,E,500010,EQUITY,0,ONLYBS,1,B\n"
)


def _bhav_for(day: dt.date) -> bytes:
    body = _BHAV_NEW_TEMPLATE.format(
        date=day.strftime("%Y-%m-%d"),
        o=100.0, h=102.0, lo=99.0, c=101.0, pc=100.0, v=20_000, tv=2_020_000,
    )
    return (body * 3).encode()  # padded past MIN_NEW_BYTES


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    init_db()
    return tmp_path


def test_refresh_universe_end_to_end(state_dir, monkeypatch):
    # The bhavcopy lookup is keyed by YYYYMMDD embedded in the URL; return
    # the same daily body for each weekday in the window.
    def fake_bhav_get(url: str) -> bytes | None:
        # Extract ymd from URL (20YYMMDD between underscores).
        import re
        m = re.search(r"_(\d{8})_F_0000\.CSV", url)
        if not m:
            return None
        ymd = m.group(1)
        day = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        if day.weekday() >= 5:
            return None
        return _bhav_for(day)

    def fake_scrip_get(url: str) -> bytes:
        return (_SCRIP_CSV * 2000).encode()

    path = refresh_universe(
        end=dt.date(2026, 4, 22),  # Wednesday
        bhavcopy_lookback_days=30,
        http_get_bhavcopy=fake_bhav_get,
        http_get_scrip=fake_scrip_get,
    )
    assert path == universe_csv_path()
    assert path.exists()

    # Provider reads exactly what was written.
    prov = CsvUniverseProvider()
    entries = prov.load()
    assert len(entries) == 1  # ONLYBS has 100-share volume, below 10k
    e = entries[0]
    assert e.symbol == "RELIANCE"
    assert e.security_id == "500325"
    assert e.exchange_segment == "BSE_EQ"
    assert e.isin == "INE002A01018"
    assert e.market_cap_cr == 0.0  # mcap filter off, column blank -> 0.0

    # Settings rows stamped.
    conn = connect()
    try:
        rows = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN (?, ?, ?)",
                (
                    SETTINGS_KEY_UNIVERSE_REFRESH_AT,
                    SETTINGS_KEY_UNIVERSE_COUNT,
                    SETTINGS_KEY_UNIVERSE_SOURCE_DATE,
                ),
            ).fetchall()
        }
    finally:
        conn.close()
    assert rows[SETTINGS_KEY_UNIVERSE_COUNT] == "1"
    assert rows[SETTINGS_KEY_UNIVERSE_SOURCE_DATE] == "2026-04-22"
    assert rows[SETTINGS_KEY_UNIVERSE_REFRESH_AT]  # non-empty


def test_refresh_raises_when_no_bhavcopy_available(state_dir):
    def none_get(url: str) -> bytes | None:
        return None

    with pytest.raises(RuntimeError):
        refresh_universe(
            end=dt.date(2026, 4, 22),
            bhavcopy_lookback_days=5,
            http_get_bhavcopy=none_get,
            http_get_scrip=lambda url: (_SCRIP_CSV * 2000).encode(),
        )
