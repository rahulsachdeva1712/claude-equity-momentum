"""Dhan CNC charge stack for BSE equity delivery. FRD B.6.

Same function is called by paper and live engines so charges match by
construction. Rates are kept here so updates require a single PR.

References: Dhan's published charges (CNC delivery) and SEBI/exchange circulars
current as of 2026-04. If Dhan changes its schedule, update RATES and bump
SCHEDULE_VERSION, then record the change in docs/FRD.md B.16.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

SCHEDULE_VERSION = "2026-04-22"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


# All rates expressed as fractions of turnover (notional = qty * price)
# unless noted. Keep these as constants for auditability.
BROKERAGE_CNC = 0.0              # Dhan CNC delivery brokerage = zero
STT_DELIVERY_SELL = 0.001        # 0.1% on sell side of delivery equity
EXCH_TXN_BSE = 0.0000375         # 0.00375%, indicative BSE group-A rate
SEBI_TURNOVER = 0.000001         # 0.0001% aka 10 / crore
STAMP_DUTY_BUY = 0.00015         # 0.015% on buy side
GST_RATE = 0.18                  # on brokerage + exch txn + SEBI


@dataclass(frozen=True)
class ChargesBreakdown:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi_turnover: float
    stamp_duty: float
    gst: float
    total: float
    schedule_version: str

    def to_dict(self) -> dict:
        return asdict(self)


def compute_charges(side: Side | str, qty: int, price: float) -> ChargesBreakdown:
    """Return the full charge breakdown for a single CNC trade leg on BSE.

    qty must be positive. `side` is BUY or SELL. Caller is responsible for
    issuing two calls (buy, then sell) when modeling a complete round-trip.
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    if price < 0:
        raise ValueError("price must be non-negative")
    side = Side(side) if not isinstance(side, Side) else side

    notional = qty * price

    brokerage = notional * BROKERAGE_CNC
    stt = notional * STT_DELIVERY_SELL if side is Side.SELL else 0.0
    exch_txn = notional * EXCH_TXN_BSE
    sebi = notional * SEBI_TURNOVER
    stamp = notional * STAMP_DUTY_BUY if side is Side.BUY else 0.0

    gst_base = brokerage + exch_txn + sebi
    gst = gst_base * GST_RATE

    total = brokerage + stt + exch_txn + sebi + stamp + gst

    return ChargesBreakdown(
        brokerage=round(brokerage, 4),
        stt=round(stt, 4),
        exchange_txn=round(exch_txn, 4),
        sebi_turnover=round(sebi, 4),
        stamp_duty=round(stamp, 4),
        gst=round(gst, 4),
        total=round(total, 4),
        schedule_version=SCHEDULE_VERSION,
    )


def non_broker_charges(breakdown: ChargesBreakdown) -> float:
    """The 'Non broker charge' column shown in the UI mockup."""
    return round(breakdown.total - breakdown.brokerage, 4)
