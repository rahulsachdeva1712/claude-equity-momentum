"""BSE bhavcopy download + parse, ported from ``research/backtest_2y``.

Two on-wire formats are supported:

- **New format** (2023-12 onwards): plain CSV, URL
  ``https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{YYYYMMDD}_F_0000.CSV``.
  Columns we need: ``TradDt``, ``FinInstrmId`` (sc_code), ``ISIN``, ``TckrSymb``,
  ``SctySrs``, ``OpnPric``, ``HghPric``, ``LwPric``, ``ClsPric``, ``PrvsClsgPric``,
  ``TtlTradgVol``, ``TtlTrfVal``. Filter rows by ``FinInstrmTp == 'STK'``.

- **Legacy format** (pre-2023-12): zipped CSV, URL
  ``https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_{DDMMYY}.zip``.
  Column names are upper-cased; equity rows carry ``SC_TYPE == 'Q'``.

Weekends and holidays return 404 from BSE — callers get ``None`` back and
should move on. This module is purely I/O + parse; the filtering (series +
ADV) lives in ``app.universe.refresh``.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

log = logging.getLogger("universe.bhavcopy")


BHAV_URL_NEW = (
    "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{ymd}_F_0000.CSV"
)
BHAV_URL_OLD = "https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_{dmy}.zip"

# BSE returns HTML 200s when the file is missing on some edges; we guard
# against that by checking the first bytes and a minimum size. The HTML
# check is the primary guard; the size floor is a belt-and-braces check
# against truncated responses. Real bhavcopies are 100 KB+, so anything
# below a few hundred bytes is definitely bogus.
_MIN_NEW_BYTES = 300
_MIN_OLD_BYTES = 200

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


_COLS_NEW = {
    "TradDt": "date",
    "FinInstrmId": "sc_code",
    "ISIN": "isin",
    "TckrSymb": "symbol",
    "SctySrs": "series",
    "OpnPric": "open",
    "HghPric": "high",
    "LwPric": "low",
    "ClsPric": "close",
    "PrvsClsgPric": "prev_close",
    "TtlTradgVol": "volume",
    "TtlTrfVal": "turnover",
}

_COLS_OLD = {
    "SC_CODE": "sc_code",
    "SC_NAME": "symbol",
    "SC_GROUP": "series",
    "ISIN_CODE": "isin",
    "OPEN": "open",
    "HIGH": "high",
    "LOW": "low",
    "CLOSE": "close",
    "PREVCLOSE": "prev_close",
    "NO_OF_SHRS": "volume",
    "NET_TURNOV": "turnover",
    "TRADING_DATE": "date",
}


def _http_get(url: str, *, timeout_s: float = 30.0) -> bytes | None:
    """GET with BSE-friendly headers. Returns None on 404 or transport error."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Referer": "https://www.bseindia.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log.warning("bhavcopy HTTP %s for %s", e.code, url)
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.warning("bhavcopy transport error for %s: %s", url, e)
        return None


def fetch_bhavcopy(
    day: dt.date,
    cache_dir: Path,
    *,
    http_get=_http_get,
) -> Path | None:
    """Download (or reuse cached) bhavcopy for ``day``.

    Returns the path to the on-disk file (new-format ``.csv`` or legacy ``.zip``),
    or ``None`` for weekend/holiday/404.

    ``http_get`` is injected for tests; signature ``(url: str) -> bytes | None``.
    """
    if day.weekday() >= 5:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    ymd = day.strftime("%Y%m%d")
    dmy = day.strftime("%d%m%y")
    new_path = cache_dir / f"bhav_{ymd}.csv"
    old_path = cache_dir / f"bhav_{ymd}.zip"
    if new_path.exists() and new_path.stat().st_size > 0:
        return new_path
    if old_path.exists() and old_path.stat().st_size > 0:
        return old_path

    # Try new format first.
    data = http_get(BHAV_URL_NEW.format(ymd=ymd))
    if data and len(data) >= _MIN_NEW_BYTES and not data[:15].lower().startswith(b"<!doctype html"):
        new_path.write_bytes(data)
        return new_path

    # Fallback: legacy zip.
    data = http_get(BHAV_URL_OLD.format(dmy=dmy))
    if data and len(data) >= _MIN_OLD_BYTES and data[:2] == b"PK":
        old_path.write_bytes(data)
        return old_path

    return None


def parse_bhavcopy(path: Path) -> pd.DataFrame:
    """Parse either-format bhavcopy into the canonical schema.

    Output columns (all strings normalized, numerics coerced):
        date (datetime64[ns]), sc_code (str), isin (str or empty), symbol (str),
        series (str), open, high, low, close, prev_close, volume, turnover (float).

    Raises ``ValueError`` on malformed input.
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _parse_new(path)
    if suffix == ".zip":
        return _parse_old(path)
    raise ValueError(f"unexpected bhavcopy file extension: {path}")


def _parse_new(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    if "FinInstrmTp" not in df.columns:
        raise ValueError(f"{path.name}: missing FinInstrmTp column (not a BSE bhavcopy?)")
    df = df[df["FinInstrmTp"] == "STK"].copy()
    missing = [c for c in _COLS_NEW if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name}: missing new-format columns {missing}")
    df = df[list(_COLS_NEW.keys())].rename(columns=_COLS_NEW)
    return _normalize(df, date_fmt=None)


def _parse_old(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"{path.name}: no csv inside zip")
        with zf.open(names[0]) as fh:
            raw = fh.read()
    df = pd.read_csv(io.BytesIO(raw), low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    if "SC_TYPE" in df.columns:
        df = df[df["SC_TYPE"].astype(str).str.strip() == "Q"].copy()
    missing = [c for c in _COLS_OLD if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name}: missing old-format columns {missing}")
    df = df[list(_COLS_OLD.keys())].rename(columns=_COLS_OLD)
    return _normalize(df, date_fmt="%d-%b-%y")


def _normalize(df: pd.DataFrame, *, date_fmt: str | None) -> pd.DataFrame:
    if date_fmt:
        df["date"] = pd.to_datetime(df["date"], format=date_fmt, errors="coerce")
    else:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("open", "high", "low", "close", "prev_close", "volume", "turnover"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["sc_code"] = df["sc_code"].astype(str).str.strip()
    df["series"] = df["series"].astype(str).str.strip()
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["isin"] = df["isin"].astype(str).str.strip().replace({"nan": "", "None": ""})
    return df.reset_index(drop=True)


def load_recent_bhavcopies(
    cache_dir: Path,
    *,
    end: dt.date,
    lookback_days: int = 35,
    http_get=_http_get,
) -> pd.DataFrame:
    """Ensure bhavcopies for roughly ``lookback_days`` trading days ending on
    ``end`` are cached, then parse and concatenate them.

    ``lookback_days`` should be generous enough to include 20 trading days
    after weekends+holidays are stripped. 35 calendar days is the default
    and comfortably covers 22 trading days.
    """
    frames: list[pd.DataFrame] = []
    for i in range(lookback_days + 1):
        d = end - dt.timedelta(days=i)
        path = fetch_bhavcopy(d, cache_dir, http_get=http_get)
        if path is None:
            continue
        try:
            frames.append(parse_bhavcopy(path))
        except ValueError as e:
            log.warning("skip malformed bhavcopy %s: %s", path.name, e)
    if not frames:
        return pd.DataFrame(
            columns=[
                "date", "sc_code", "isin", "symbol", "series",
                "open", "high", "low", "close", "prev_close", "volume", "turnover",
            ]
        )
    return pd.concat(frames, ignore_index=True)
