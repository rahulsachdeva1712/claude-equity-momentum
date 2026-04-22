"""Live trading engine. FRD B.7.

Places Dhan CNC market orders tagged with a correlationId, polls for
terminal status, records fills, and produces the qty_override map the
paper engine consumes to stay in lockstep (parity).

Kill-switch semantics: placement loop checks live_enabled before every
order. Flipping off mid-batch halts placement (existing in-flight
orders continue to terminal).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import date
from typing import Iterable

from app.alerts import Alert, raise_alert
from app.charges import Side, compute_charges
from app.db import tx
from app.dhan.client import DhanClient
from app.dhan.errors import DhanAuthError, DhanError, DhanRejected, DhanUnavailable
from app.dhan.models import OrderStatus, PlaceOrderRequest
from app.time_utils import now_ist

log = logging.getLogger("live")

TAG_PREFIX = "emrb"
TERMINAL = {"TRADED", "REJECTED", "CANCELLED"}
SETTINGS_KEY_LIVE_ENABLED = "live_enabled"


def is_live_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY_LIVE_ENABLED,)
    ).fetchone()
    return bool(row and row["value"] == "1")


def set_live_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (SETTINGS_KEY_LIVE_ENABLED, "1" if enabled else "0", now_ist().isoformat()),
        )


def make_correlation_id(session_date: date, symbol: str, action: str) -> str:
    return f"{TAG_PREFIX}:{session_date.isoformat()}:{symbol}:{action}:{uuid.uuid4().hex[:8]}"


async def place_orders(
    conn: sqlite3.Connection,
    dhan: DhanClient,
    session_date: date,
    paper_orders: Iterable[tuple[int, str, str, int, str, str]],
) -> dict[int, int]:
    """Place a live Dhan order for each paper order while live is enabled.

    paper_orders: iterable of (paper_order_id, symbol, action, qty, security_id, exchange_segment).
    Returns qty_override map (paper_order_id -> actual filled qty) that the
    paper engine should use when executing. Rejected / skipped paper orders
    map to 0; untouched ones are absent.

    Orders are placed sequentially so the kill switch can short-circuit the
    loop deterministically. If that becomes a latency issue the loop can
    be parallelized with a bounded gather, re-checking the flag on each
    completion — out of scope for v1.
    """
    overrides: dict[int, int] = {}

    for paper_id, symbol, action, qty, security_id, segment in paper_orders:
        if not is_live_enabled(conn):
            log.info("live disabled mid-run, stopping placement")
            raise_alert(
                conn,
                Alert(
                    severity="info",
                    source="live",
                    message="live disabled mid-run; placement halted",
                ),
            )
            break

        side = "SELL" if action in ("TRIM", "EXIT") else "BUY"
        corr = make_correlation_id(session_date, symbol, action)

        with tx(conn):
            cur = conn.execute(
                "INSERT INTO live_orders"
                " (session_date, symbol, action, order_qty, correlation_id, status, placed_at)"
                " VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
                (session_date.isoformat(), symbol, action, qty, corr, now_ist().isoformat()),
            )
            live_order_id = int(cur.lastrowid)

        req = PlaceOrderRequest(
            security_id=security_id,
            exchange_segment=segment,
            transaction_type=side,
            quantity=qty,
            correlation_id=corr,
        )

        request_json = json.dumps(
            {
                "securityId": security_id,
                "exchangeSegment": segment,
                "transactionType": side,
                "quantity": qty,
                "correlationId": corr,
                "productType": "CNC",
                "orderType": "MARKET",
            }
        )

        try:
            dhan_oid = await dhan.place_order(req)
        except DhanRejected as e:
            _record_reject(conn, live_order_id, session_date, corr, symbol, str(e), e.payload, request_json)
            overrides[paper_id] = 0
            continue
        except DhanAuthError as e:
            _record_reject(conn, live_order_id, session_date, corr, symbol, f"auth error: {e}", e.payload, request_json)
            raise_alert(
                conn,
                Alert(severity="error", source="live", message=f"auth error placing {symbol}; disabling live"),
            )
            set_live_enabled(conn, False)
            overrides[paper_id] = 0
            break
        except DhanUnavailable as e:
            _record_reject(conn, live_order_id, session_date, corr, symbol, f"unavailable: {e}", e.payload, request_json)
            overrides[paper_id] = 0
            continue
        except DhanError as e:
            _record_reject(conn, live_order_id, session_date, corr, symbol, str(e), e.payload, request_json)
            overrides[paper_id] = 0
            continue

        with tx(conn):
            conn.execute(
                "UPDATE live_orders SET dhan_order_id = ?, status = 'TRANSIT' WHERE id = ?",
                (dhan_oid, live_order_id),
            )
            conn.execute(
                "INSERT INTO audit_log (session_date, correlation_id, kind, request_json, response_json, http_status, created_at)"
                " VALUES (?, ?, 'PLACE', ?, ?, 200, ?)",
                (
                    session_date.isoformat(),
                    corr,
                    request_json,
                    json.dumps({"orderId": dhan_oid}),
                    now_ist().isoformat(),
                ),
            )

        # Wait for terminal status. Poll every second up to a timeout.
        status = await _wait_for_terminal(dhan, dhan_oid, timeout_s=60)
        _record_terminal(conn, live_order_id, session_date, corr, symbol, action, status)

        if status.status == "TRADED" and status.filled_qty > 0:
            overrides[paper_id] = status.filled_qty
        else:
            overrides[paper_id] = 0
            if status.status == "REJECTED":
                raise_alert(
                    conn,
                    Alert(
                        severity="error",
                        source="live",
                        message=f"order rejected: {symbol} {action} x{qty}",
                        payload={"reason": status.reject_reason or "", "correlation_id": corr},
                    ),
                )

    return overrides


async def _wait_for_terminal(dhan: DhanClient, order_id: str, timeout_s: int) -> OrderStatus:
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: OrderStatus | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            last = await dhan.order_status(order_id)
        except DhanUnavailable:
            await asyncio.sleep(1.0)
            continue
        if last.status in TERMINAL:
            return last
        await asyncio.sleep(1.0)
    # Timed out: treat as still OPEN — caller records what we have.
    return last or OrderStatus(
        dhan_order_id=order_id, status="OPEN", filled_qty=0, ordered_qty=0,
        average_price=0.0, reject_reason=None, correlation_id=None, raw={},
    )


def _record_reject(
    conn: sqlite3.Connection,
    live_order_id: int,
    session_date: date,
    correlation_id: str,
    symbol: str,
    reason: str,
    payload: dict,
    request_json: str,
) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE live_orders SET status = 'REJECTED', reject_reason = ?, terminal_at = ? WHERE id = ?",
            (reason, now_ist().isoformat(), live_order_id),
        )
        conn.execute(
            "INSERT INTO audit_log (session_date, correlation_id, kind, request_json, response_json, http_status, created_at)"
            " VALUES (?, ?, 'PLACE', ?, ?, ?, ?)",
            (
                session_date.isoformat(),
                correlation_id,
                request_json,
                json.dumps(payload or {}),
                payload.get("status") if isinstance(payload, dict) else None,
                now_ist().isoformat(),
            ),
        )


def _record_terminal(
    conn: sqlite3.Connection,
    live_order_id: int,
    session_date: date,
    correlation_id: str,
    symbol: str,
    action: str,
    status: OrderStatus,
) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE live_orders SET status = ?, terminal_at = ? WHERE id = ?",
            (status.status, now_ist().isoformat(), live_order_id),
        )
        if status.status == "TRADED" and status.filled_qty > 0:
            side = Side.BUY if action == "BUY" else Side.SELL
            price = status.average_price
            charges = compute_charges(side, status.filled_qty, price)
            conn.execute(
                "INSERT INTO live_fills"
                " (live_order_id, session_date, symbol, side, fill_qty, fill_price, charges_total, charges_json, filled_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    live_order_id,
                    session_date.isoformat(),
                    symbol,
                    side.value,
                    int(status.filled_qty),
                    float(price),
                    charges.total,
                    json.dumps(charges.to_dict()),
                    now_ist().isoformat(),
                ),
            )
