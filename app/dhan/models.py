"""Request/response shapes for the Dhan v2 API.

These are intentionally minimal and mirror only the fields the app uses.
If Dhan responds with extra fields, they are passed through as raw_json
on the calling side. If Dhan renames a field, change it here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional


@dataclass(frozen=True)
class OHLCBar:
    symbol_id: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class PlaceOrderRequest:
    security_id: str
    exchange_segment: str   # e.g. "BSE_EQ"
    transaction_type: str   # "BUY" / "SELL"
    quantity: int
    product_type: str = "CNC"
    order_type: str = "MARKET"
    price: float = 0.0      # ignored for MARKET
    trigger_price: float = 0.0
    validity: str = "DAY"
    correlation_id: str = ""


@dataclass(frozen=True)
class OrderStatus:
    dhan_order_id: str
    status: str             # PENDING / TRANSIT / OPEN / TRADED / REJECTED / CANCELLED
    filled_qty: int
    ordered_qty: int
    average_price: float
    reject_reason: Optional[str]
    correlation_id: Optional[str]
    raw: dict


@dataclass(frozen=True)
class Position:
    symbol: str
    security_id: str
    exchange_segment: str
    net_qty: int
    avg_cost: float
    ltp: float
    unrealized_pnl: float
    realized_pnl: float
    raw: dict
