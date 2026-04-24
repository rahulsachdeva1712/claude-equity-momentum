"""CsvUniverseProvider — the read-seam between the refresh artifact and the
strategy engine. Tests accept the full Champion B schema and also stay
backward-compatible with the legacy 4-column hand-written CSVs so users
migrating don't break overnight.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.strategy.universe import CsvUniverseProvider, UniverseEntry


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_empty_file_returns_empty(tmp_path):
    p = tmp_path / "universe.csv"
    p.write_text("symbol,security_id,exchange_segment,market_cap_cr\n", encoding="utf-8")
    assert CsvUniverseProvider(p).load() == []


def test_missing_file_returns_empty(tmp_path):
    p = tmp_path / "missing.csv"
    assert CsvUniverseProvider(p).load() == []


def test_new_schema_roundtrip(tmp_path):
    p = tmp_path / "universe.csv"
    _write(
        p,
        "symbol,security_id,exchange_segment,market_cap_cr,isin,sc_code,series,adv_20d\n"
        "RELIANCE,500325,BSE_EQ,,INE002A01018,500325,A,1500000\n"
        "HDFCBANK,500180,BSE_EQ,,INE040A01034,500180,A,950000\n",
    )
    entries = CsvUniverseProvider(p).load()
    assert len(entries) == 2
    reliance = entries[0]
    assert reliance.symbol == "RELIANCE"
    assert reliance.security_id == "500325"
    assert reliance.exchange_segment == "BSE_EQ"
    assert reliance.market_cap_cr == 0.0  # blank -> 0.0 (mcap filter off)
    assert reliance.isin == "INE002A01018"
    assert reliance.sc_code == "500325"


def test_legacy_schema_still_loads(tmp_path):
    """Pre-Champion-B hand-written CSVs keep working during migration."""
    p = tmp_path / "universe.csv"
    _write(
        p,
        "symbol,security_id,exchange_segment,market_cap_cr\n"
        "RELIANCE,500325,BSE_EQ,1500.0\n"
        "HDFCBANK,500180,BSE_EQ,800.0\n",
    )
    entries = CsvUniverseProvider(p).load()
    assert {e.symbol for e in entries} == {"RELIANCE", "HDFCBANK"}
    assert entries[0].market_cap_cr == 1500.0
    assert entries[0].isin == ""  # absent from legacy file


def test_exchange_segment_defaults_to_bse_eq(tmp_path):
    p = tmp_path / "universe.csv"
    _write(
        p,
        "symbol,security_id,exchange_segment,market_cap_cr\n"
        "RELIANCE,500325,,\n",  # blank exchange_segment
    )
    entries = CsvUniverseProvider(p).load()
    assert entries[0].exchange_segment == "BSE_EQ"


def test_skips_rows_missing_symbol_or_security_id(tmp_path):
    p = tmp_path / "universe.csv"
    _write(
        p,
        "symbol,security_id,exchange_segment,market_cap_cr\n"
        ",500325,BSE_EQ,\n"            # blank symbol
        "RELIANCE,,BSE_EQ,\n"          # blank security_id
        "RELIANCE,500325,BSE_EQ,\n",   # ok
    )
    entries = CsvUniverseProvider(p).load()
    assert len(entries) == 1
    assert entries[0].symbol == "RELIANCE"


def test_skips_rows_with_invalid_mcap(tmp_path):
    p = tmp_path / "universe.csv"
    _write(
        p,
        "symbol,security_id,exchange_segment,market_cap_cr\n"
        "BAD,500001,BSE_EQ,not-a-number\n"
        "OK,500002,BSE_EQ,500.0\n",
    )
    entries = CsvUniverseProvider(p).load()
    # Bad row is skipped (ValueError caught). Only OK remains.
    assert [e.symbol for e in entries] == ["OK"]
