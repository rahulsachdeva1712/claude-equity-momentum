"""Universe provider. FRD A.2.

The FRD's `all_bse_equities` universe needs an authoritative list of BSE
symbols with security_id, exchange_segment, and current market_cap_cr.
Rather than shipping a stale snapshot, the provider is pluggable:

- `CsvUniverseProvider(path)` reads a CSV the user maintains.
- Default CSV path is ~/.claude-equity-momentum/universe.csv.

CSV columns (header required): symbol, security_id, exchange_segment, market_cap_cr.

Future: add DhanScripMasterProvider that downloads the scrip master and
computes market cap from daily close * shares outstanding.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from app.paths import state_dir


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    security_id: str
    exchange_segment: str
    market_cap_cr: float


class UniverseProvider(Protocol):
    def load(self) -> list[UniverseEntry]: ...


class CsvUniverseProvider:
    def __init__(self, path: Path | None = None):
        self.path = path or (state_dir() / "universe.csv")

    def load(self) -> list[UniverseEntry]:
        if not self.path.exists():
            return []
        out: list[UniverseEntry] = []
        with self.path.open("r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    out.append(
                        UniverseEntry(
                            symbol=row["symbol"].strip(),
                            security_id=row["security_id"].strip(),
                            exchange_segment=row.get("exchange_segment", "BSE_EQ").strip() or "BSE_EQ",
                            market_cap_cr=float(row["market_cap_cr"]),
                        )
                    )
                except (KeyError, ValueError):
                    continue
        return out


def default_provider() -> UniverseProvider:
    return CsvUniverseProvider()
