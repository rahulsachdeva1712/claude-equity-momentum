"""Dhan scrip-master download + parse.

Dhan publishes a daily scrip master as a public CSV (no auth) at
``https://images.dhan.co/api-data/api-scrip-master.csv`` — used here to
bridge the bhavcopy's BSE security code into Dhan's ``security_id`` +
``exchange_segment``. For BSE equities the two codes are identical
(bhavcopy's ``FinInstrmId`` == Dhan's ``SEM_SMST_SECURITY_ID``), so the
join is on ``sc_code``. ISIN is not present in Dhan's published file and
is therefore optional here — when absent we leave the column empty and
still join successfully.

The scrip master's column names have drifted across Dhan's doc updates, so
the parser is tolerant: it discovers the right columns by a set of known
aliases and fails loudly only when no alias matches. That way a Dhan-side
rename shows up as a single fix here, not a silent provider outage.

Two environment overrides for operations:
- ``DHAN_SCRIP_MASTER_URL`` — override the upstream URL.
- ``DHAN_SCRIP_MASTER_PATH`` — point at a local pre-fetched CSV (useful for
  restricted networks and for tests).
"""
from __future__ import annotations

import io
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

log = logging.getLogger("universe.scrip_master")


SCRIP_MASTER_URL_DEFAULT = "https://images.dhan.co/api-data/api-scrip-master.csv"

# Column aliases: scrip master CSV columns we need, in order of preference.
# The first alias found in the CSV wins.
_COL_ALIASES: dict[str, tuple[str, ...]] = {
    "security_id": ("SEM_SMST_SECURITY_ID", "SECURITY_ID"),
    "exch_id": ("SEM_EXM_EXCH_ID", "EXCHANGE", "EXCH"),
    "segment": ("SEM_SEGMENT", "SEGMENT"),
    "instrument": ("SEM_INSTRUMENT_NAME", "SEM_EXCH_INSTRUMENT_TYPE", "INSTRUMENT_TYPE"),
    "isin": ("SM_ISIN", "SEM_ISIN", "ISIN"),
    "series": ("SEM_SERIES", "SERIES"),
    "trading_symbol": ("SEM_TRADING_SYMBOL", "TRADING_SYMBOL", "SYMBOL_NAME"),
    "lot_size": ("SEM_LOT_UNITS", "LOT_SIZE", "SM_LOT_SIZE"),
}


@dataclass(frozen=True)
class ScripEntry:
    security_id: str
    exchange_segment: str  # e.g. "BSE_EQ"
    isin: str
    trading_symbol: str
    series: str
    lot_size: int


class ScripMasterError(RuntimeError):
    pass


def _http_get(url: str, *, timeout_s: float = 60.0) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
        log.warning("scrip master fetch failed for %s: %s", url, e)
        return None


def fetch_scrip_master(
    cache_dir: Path,
    *,
    url: str | None = None,
    max_age_s: float = 7 * 24 * 3600,  # one week
    http_get=_http_get,
) -> Path:
    """Return a path to a fresh scrip-master CSV.

    Reuses a cached copy younger than ``max_age_s``. Falls back to a cached
    copy of any age if a refresh fails — callers would rather have a slightly
    stale scrip map than fail the entire universe refresh on a flaky mirror.

    Honors ``DHAN_SCRIP_MASTER_PATH`` as a static local override and
    ``DHAN_SCRIP_MASTER_URL`` for the upstream URL.
    """
    override_path = os.environ.get("DHAN_SCRIP_MASTER_PATH")
    if override_path:
        p = Path(override_path).expanduser()
        if not p.exists():
            raise ScripMasterError(f"DHAN_SCRIP_MASTER_PATH points at missing file: {p}")
        return p

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "scrip_master.csv"
    effective_url = url or os.environ.get("DHAN_SCRIP_MASTER_URL") or SCRIP_MASTER_URL_DEFAULT

    fresh = cache_path.exists() and cache_path.stat().st_size > 0
    stale = (
        fresh
        and (os.path.getmtime(cache_path) + max_age_s) < __import__("time").time()
    )
    if fresh and not stale:
        return cache_path

    data = http_get(effective_url)
    if data and len(data) > 100_000:
        cache_path.write_bytes(data)
        return cache_path

    # Refresh failed. If we have any cached copy, use it and log.
    if cache_path.exists() and cache_path.stat().st_size > 0:
        log.warning("scrip master refresh failed; reusing stale cache at %s", cache_path)
        return cache_path
    raise ScripMasterError(f"scrip master download failed and no cache available at {cache_path}")


def _resolve_columns(columns: Iterable[str]) -> dict[str, str]:
    lookup = {c.strip(): c for c in columns}
    resolved: dict[str, str] = {}
    for canonical, aliases in _COL_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                resolved[canonical] = lookup[alias]
                break
    # Dhan's live scrip master does NOT publish ISIN (the SM_ISIN column
    # that's sometimes referenced in older docs isn't there). We keep it
    # optional — the bhavcopy-to-scrip join runs on sc_code, not ISIN.
    required = {"security_id", "trading_symbol", "series"}
    missing = required - resolved.keys()
    if missing:
        raise ScripMasterError(
            f"scrip master missing required columns {sorted(missing)}; "
            f"have={sorted(lookup.keys())[:20]}..."
        )
    return resolved


def load_bse_equities(path: Path) -> pd.DataFrame:
    """Parse the scrip master into BSE_EQ rows only.

    Output columns: ``security_id`` (str, == BSE sc_code for BSE equities),
    ``exchange_segment`` (str, always ``BSE_EQ``), ``isin`` (str, empty when
    the source doesn't carry it), ``trading_symbol`` (str), ``series`` (str),
    ``lot_size`` (int, 1 when the source doesn't carry it).
    """
    df = pd.read_csv(path, low_memory=False, dtype=str)
    cols = _resolve_columns(df.columns)

    # Filter to BSE equity rows. The scrip master identifies exchange via
    # ``SEM_EXM_EXCH_ID`` (BSE/NSE) and segment via ``SEM_SEGMENT`` ("E" for
    # equity) + ``SEM_INSTRUMENT_NAME`` ("EQUITY"). Guard against either
    # pair being missing by filtering on whatever we have.
    if "exch_id" in cols:
        df = df[df[cols["exch_id"]].astype(str).str.strip().str.upper() == "BSE"]
    if "segment" in cols:
        df = df[df[cols["segment"]].astype(str).str.strip().str.upper().isin({"E", "EQ"})]
    if "instrument" in cols:
        df = df[df[cols["instrument"]].astype(str).str.strip().str.upper().isin({"EQUITY", "EQ"})]

    out = pd.DataFrame(
        {
            "security_id": df[cols["security_id"]].astype(str).str.strip(),
            "trading_symbol": df[cols["trading_symbol"]].astype(str).str.strip(),
            "series": df[cols["series"]].astype(str).str.strip(),
        }
    )
    if "isin" in cols:
        out["isin"] = df[cols["isin"]].astype(str).str.strip().replace({"nan": "", "None": ""})
    else:
        out["isin"] = ""
    if "lot_size" in cols:
        lot = pd.to_numeric(df[cols["lot_size"]], errors="coerce").fillna(1).astype(int)
        out["lot_size"] = lot.to_numpy()
    else:
        out["lot_size"] = 1
    out["exchange_segment"] = "BSE_EQ"

    # Drop rows without a security_id (can't place orders without it).
    out = out[out["security_id"].str.len() > 0].copy()
    # Dedupe on security_id (ISIN may be missing so it's not a safe key).
    out = out.drop_duplicates(subset=["security_id"], keep="first").reset_index(drop=True)
    return out


def build_scrip_index(path: Path) -> dict[str, ScripEntry]:
    """Convenience: return ``{security_id: ScripEntry}`` for BSE equity rows.

    BSE equities use ``security_id == sc_code``, so this is the natural key
    for joining against a bhavcopy frame.
    """
    df = load_bse_equities(path)
    out: dict[str, ScripEntry] = {}
    for _, r in df.iterrows():
        out[r["security_id"]] = ScripEntry(
            security_id=r["security_id"],
            exchange_segment=r["exchange_segment"],
            isin=r.get("isin", "") or "",
            trading_symbol=r["trading_symbol"],
            series=r["series"],
            lot_size=int(r["lot_size"]),
        )
    return out


# Back-compat alias: the original design used ISIN as the bridge key. A few
# call sites (and older tests) still reference ``build_isin_index``. Keep
# the name pointed at the sc_code-based builder so they don't break, but
# note that it is keyed on security_id now (ISIN isn't available in the
# upstream Dhan CSV).
build_isin_index = build_scrip_index
