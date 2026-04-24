"""Universe provider. FRD A.2.

The provider is the single read-seam between the daily universe-refresh
pipeline (``app.universe.refresh``) and the strategy engine. The canonical
on-disk artifact is ``<state_dir>/universe/universe.csv``, produced by the
worker's nightly ``universe_refresh_job``.

CSV columns (header required):
  symbol, security_id, exchange_segment, market_cap_cr, isin, sc_code,
  series, adv_20d

``market_cap_cr`` may be blank: Champion B runs with the market-cap filter
off, matching what the 2-year backtest actually tested (bhavcopy carries no
market-cap data). When blank, entries are loaded with ``market_cap_cr=0.0``
and the strategy's ``use_mcap_filter`` flag (A.5) gates whether that zero
is checked. Re-enabling the filter is a single config flip plus populating
the column in the refresh step.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.universe.refresh import universe_csv_path


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    security_id: str
    exchange_segment: str
    market_cap_cr: float
    isin: str = ""
    sc_code: str = ""


class UniverseProvider(Protocol):
    def load(self) -> list[UniverseEntry]: ...


class CsvUniverseProvider:
    """Read the universe CSV written by ``app.universe.refresh``.

    The legacy one-column layout (``symbol, security_id, exchange_segment,
    market_cap_cr``) is still accepted — missing columns fall back to sane
    defaults so a hand-maintained CSV keeps working during migration.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or universe_csv_path()

    def load(self) -> list[UniverseEntry]:
        if not self.path.exists():
            return []
        out: list[UniverseEntry] = []
        with self.path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    sym = (row.get("symbol") or "").strip()
                    sec = (row.get("security_id") or "").strip()
                    if not sym or not sec:
                        continue
                    mcap_raw = (row.get("market_cap_cr") or "").strip()
                    mcap = float(mcap_raw) if mcap_raw else 0.0
                    out.append(
                        UniverseEntry(
                            symbol=sym,
                            security_id=sec,
                            exchange_segment=(row.get("exchange_segment") or "BSE_EQ").strip() or "BSE_EQ",
                            market_cap_cr=mcap,
                            isin=(row.get("isin") or "").strip(),
                            sc_code=(row.get("sc_code") or "").strip(),
                        )
                    )
                except (KeyError, ValueError):
                    continue
        return out


def default_provider() -> UniverseProvider:
    return CsvUniverseProvider()
