"""Dhan scrip-master parse. Tolerant to Dhan-side column renames — the
parser uses alias lookups, so these tests cover the alias fallbacks too.

Reality note: Dhan's published scrip master does NOT carry ISIN. The
parser tolerates either presence or absence, and the bhavcopy-to-scrip
join runs on security_id (which equals the BSE sc_code for BSE equities),
not on ISIN.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.universe.scrip_master import (
    ScripMasterError,
    build_isin_index,
    build_scrip_index,
    fetch_scrip_master,
    load_bse_equities,
)


# Real Dhan scrip master schema — no ISIN column.
_MODERN_CSV = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
    "SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_SERIES\n"
    "BSE,E,500325,EQUITY,0,RELIANCE,1,A\n"
    "NSE,E,2885,EQUITY,0,RELIANCE,1,EQ\n"
    "BSE,D,846025,FUTSTK,0,RELIANCEFUT,505,XX\n"
    "BSE,E,500180,EQUITY,0,HDFCBANK,1,A\n"
)

# Older/alternate schema with ISIN present and alternate column aliases.
_LEGACY_CSV = (
    "EXCHANGE,SEGMENT,SECURITY_ID,INSTRUMENT_TYPE,TRADING_SYMBOL,SERIES,ISIN,LOT_SIZE\n"
    "BSE,E,500325,EQUITY,RELIANCE,A,INE002A01018,1\n"
    "BSE,E,500180,EQUITY,HDFCBANK,A,INE040A01034,1\n"
)


def test_load_modern_filters_to_bse_equity(tmp_path):
    p = tmp_path / "scrip.csv"
    p.write_text(_MODERN_CSV)
    df = load_bse_equities(p)
    # Both BSE equities survive; NSE row and BSE future are filtered out.
    assert set(df["trading_symbol"]) == {"RELIANCE", "HDFCBANK"}
    assert (df["exchange_segment"] == "BSE_EQ").all()
    assert set(df["series"]) == {"A"}
    reliance = df.loc[df["trading_symbol"] == "RELIANCE"].iloc[0]
    assert reliance["security_id"] == "500325"
    # No ISIN in the real Dhan feed — column present but blank.
    assert reliance["isin"] == ""


def test_load_legacy_alias_schema(tmp_path):
    p = tmp_path / "scrip.csv"
    p.write_text(_LEGACY_CSV)
    df = load_bse_equities(p)
    assert set(df["trading_symbol"]) == {"RELIANCE", "HDFCBANK"}
    assert (df["security_id"].astype(str).str.len() > 0).all()
    # When the source does carry ISIN, we pass it through.
    assert df.loc[df["trading_symbol"] == "RELIANCE", "isin"].iloc[0] == "INE002A01018"


def test_missing_required_column_raises(tmp_path):
    p = tmp_path / "scrip.csv"
    p.write_text("COL_A,COL_B\n1,2\n")
    with pytest.raises(ScripMasterError):
        load_bse_equities(p)


def test_build_scrip_index_keyed_on_security_id(tmp_path):
    p = tmp_path / "scrip.csv"
    p.write_text(_MODERN_CSV)
    idx = build_scrip_index(p)
    assert "500325" in idx
    entry = idx["500325"]
    assert entry.security_id == "500325"
    assert entry.exchange_segment == "BSE_EQ"
    assert entry.trading_symbol == "RELIANCE"
    # No ISIN in modern Dhan feed.
    assert entry.isin == ""


def test_build_isin_index_is_backcompat_alias(tmp_path):
    """Older callers used build_isin_index; it now delegates to build_scrip_index
    (keyed on security_id) since Dhan dropped ISIN from the published file."""
    p = tmp_path / "scrip.csv"
    p.write_text(_MODERN_CSV)
    assert build_isin_index(p) == build_scrip_index(p)


def test_fetch_uses_override_path(tmp_path, monkeypatch):
    override = tmp_path / "my_scrip.csv"
    override.write_text(_MODERN_CSV)
    monkeypatch.setenv("DHAN_SCRIP_MASTER_PATH", str(override))
    out = fetch_scrip_master(tmp_path / "cache")
    assert out == override


def test_fetch_rejects_missing_override_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DHAN_SCRIP_MASTER_PATH", str(tmp_path / "missing.csv"))
    with pytest.raises(ScripMasterError):
        fetch_scrip_master(tmp_path / "cache")


def test_fetch_reuses_fresh_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cached = cache_dir / "scrip_master.csv"
    cached.write_text(_MODERN_CSV * 1000)  # big enough

    def should_not_be_called(url: str):
        raise AssertionError("http_get called despite fresh cache")

    out = fetch_scrip_master(cache_dir, http_get=should_not_be_called)
    assert out == cached


def test_fetch_downloads_when_cache_missing(tmp_path):
    calls: list[str] = []

    def fake_get(url: str) -> bytes:
        calls.append(url)
        # Simulate a download of adequate size.
        return (_MODERN_CSV * 3000).encode()

    out = fetch_scrip_master(tmp_path / "cache", http_get=fake_get)
    assert out.exists()
    assert len(calls) == 1


def test_fetch_falls_back_to_stale_cache_on_error(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cached = cache_dir / "scrip_master.csv"
    cached.write_text(_MODERN_CSV)
    # Make the cache look older than max_age_s.
    import os
    import time
    old = time.time() - 365 * 24 * 3600
    os.utime(cached, (old, old))

    def failing_get(url: str) -> bytes | None:
        return None  # download fails

    out = fetch_scrip_master(cache_dir, http_get=failing_get)
    assert out == cached  # stale fallback used
