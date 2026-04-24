"""Dhan v2 HTTP client. Async. One instance per worker process.

Notes for the user:
- Access token expiry is parsed from the JWT `exp` claim.
- Paths and field names target the documented Dhan v2 REST API. If Dhan
  renames a field on their side, change the constant in this file.
- No retries inside method bodies; callers (scheduler/recon) use tenacity
  with explicit policies so retry behavior is visible in one place.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt

from app.dhan.errors import DhanAuthError, DhanError, DhanRejected, DhanUnavailable
from app.dhan.models import OHLCBar, OrderStatus, PlaceOrderRequest, Position

log = logging.getLogger("dhan")

_PATHS = {
    "fund_limit": "/fundlimit",
    "historical_daily": "/charts/historical",
    "intraday": "/charts/intraday",
    "place_order": "/orders",
    "order_status": "/orders/{order_id}",
    "order_book": "/orders",
    "positions": "/positions",
}


def jwt_expiry_epoch(token: str) -> int | None:
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001
        return None
    exp = claims.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def jwt_seconds_to_expiry(token: str, now: datetime | None = None) -> int | None:
    exp = jwt_expiry_epoch(token)
    if exp is None:
        return None
    n = int((now or datetime.now(timezone.utc)).timestamp())
    return exp - n


class DhanClient:
    def __init__(self, base_url: str, client_id: str, access_token: str, timeout_s: float = 10.0):
        self._base = base_url.rstrip("/")
        self._client_id = client_id
        self._access_token = access_token
        self._http = httpx.AsyncClient(timeout=timeout_s)

    async def close(self) -> None:
        await self._http.aclose()

    def set_access_token(self, token: str) -> None:
        self._access_token = token

    def _headers(self) -> dict[str, str]:
        return {
            "access-token": self._access_token,
            "client-id": self._client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None) -> dict:
        url = f"{self._base}{path}"
        try:
            r = await self._http.request(method, url, headers=self._headers(), json=json_body, params=params)
        except httpx.TransportError as e:
            raise DhanUnavailable(f"transport error: {e}") from e

        if r.status_code == 401:
            raise DhanAuthError("unauthorized", status=401, payload=_safe_json(r))
        if r.status_code >= 500:
            raise DhanUnavailable(f"server {r.status_code}", status=r.status_code, payload=_safe_json(r))
        if r.status_code >= 400:
            raise DhanError(f"client {r.status_code}", status=r.status_code, payload=_safe_json(r))

        return _safe_json(r)

    # ------- endpoints -------

    async def validate_token(self) -> bool:
        try:
            await self._request("GET", _PATHS["fund_limit"])
            return True
        except DhanAuthError:
            return False

    async def market_status(self) -> str:
        """Returns 'OPEN' during IST weekday market hours, 'CLOSED' otherwise.

        Dhan retired the `/v2/marketfeed/marketstatus` endpoint with no
        drop-in replacement in the v2 docs, so this falls back to a local
        IST-clock + weekday check. This matches FRD B.5's "no pre-baked
        holiday calendar" constraint: holidays that fall on weekdays will
        read 'OPEN' here, but downstream safety gates (intraday candle
        fetch, order placement) fail-close on non-trading days.
        """
        from app.time_utils import is_market_hours

        return "OPEN" if is_market_hours() else "CLOSED"

    async def historical_daily(
        self,
        security_id: str,
        exchange_segment: str,
        from_date: str,
        to_date: str,
    ) -> list[OHLCBar]:
        body = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": "EQUITY",
            "fromDate": from_date,
            "toDate": to_date,
        }
        data = await self._request("POST", _PATHS["historical_daily"], json_body=body)
        return _parse_candles(security_id, data)

    async def intraday(
        self,
        security_id: str,
        exchange_segment: str,
        interval_minutes: int,
        from_iso: str,
        to_iso: str,
    ) -> list[OHLCBar]:
        body = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": "EQUITY",
            "interval": str(interval_minutes),
            "fromDate": from_iso,
            "toDate": to_iso,
        }
        data = await self._request("POST", _PATHS["intraday"], json_body=body)
        return _parse_candles(security_id, data)

    async def place_order(self, req: PlaceOrderRequest) -> str:
        body = {
            "dhanClientId": self._client_id,
            "correlationId": req.correlation_id,
            "transactionType": req.transaction_type,
            "exchangeSegment": req.exchange_segment,
            "productType": req.product_type,
            "orderType": req.order_type,
            "validity": req.validity,
            "securityId": req.security_id,
            "quantity": req.quantity,
            "price": req.price,
            "triggerPrice": req.trigger_price,
        }
        data = await self._request("POST", _PATHS["place_order"], json_body=body)
        if data.get("orderStatus") == "REJECTED":
            raise DhanRejected(data.get("remarks") or "rejected by Dhan", payload=data)
        order_id = data.get("orderId") or (data.get("data") or {}).get("orderId")
        if not order_id:
            raise DhanError("no orderId in place_order response", payload=data)
        return str(order_id)

    async def order_status(self, order_id: str) -> OrderStatus:
        data = await self._request("GET", _PATHS["order_status"].format(order_id=order_id))
        row = data if "orderId" in data else (data.get("data") or {})
        return OrderStatus(
            dhan_order_id=str(row.get("orderId") or order_id),
            status=str(row.get("orderStatus") or "UNKNOWN").upper(),
            filled_qty=int(row.get("filledQty") or 0),
            ordered_qty=int(row.get("quantity") or 0),
            average_price=float(row.get("averageTradedPrice") or 0.0),
            reject_reason=row.get("remarks") if row.get("orderStatus") == "REJECTED" else None,
            correlation_id=row.get("correlationId"),
            raw=row if isinstance(row, dict) else {},
        )

    async def positions(self) -> list[Position]:
        data = await self._request("GET", _PATHS["positions"])
        rows = data if isinstance(data, list) else (data.get("data") or [])
        out: list[Position] = []
        for r in rows:
            out.append(
                Position(
                    symbol=str(r.get("tradingSymbol") or r.get("symbol") or ""),
                    security_id=str(r.get("securityId") or ""),
                    exchange_segment=str(r.get("exchangeSegment") or ""),
                    net_qty=int(r.get("netQty") or 0),
                    avg_cost=float(r.get("buyAvg") or r.get("avgCostPrice") or 0.0),
                    ltp=float(r.get("ltp") or r.get("lastTradedPrice") or 0.0),
                    unrealized_pnl=float(r.get("unrealizedProfit") or 0.0),
                    realized_pnl=float(r.get("realizedProfit") or 0.0),
                    raw=r,
                )
            )
        return out


def _safe_json(r: httpx.Response) -> dict:
    try:
        return r.json() if r.content else {}
    except json.JSONDecodeError:
        return {"_non_json": r.text[:500]}


def _parse_candles(security_id: str, data: dict) -> list[OHLCBar]:
    """Dhan candle response is column-oriented: timestamp, open, high, low, close, volume."""
    container = data.get("data", data)
    ts = container.get("timestamp") or container.get("start_Time") or []
    op = container.get("open") or []
    hi = container.get("high") or []
    lo = container.get("low") or []
    cl = container.get("close") or []
    vol = container.get("volume") or []
    n = min(len(ts), len(op), len(hi), len(lo), len(cl))
    out: list[OHLCBar] = []
    for i in range(n):
        t = ts[i]
        if isinstance(t, (int, float)):
            dt = datetime.fromtimestamp(t, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        out.append(
            OHLCBar(
                symbol_id=security_id,
                ts=dt,
                open=float(op[i]),
                high=float(hi[i]),
                low=float(lo[i]),
                close=float(cl[i]),
                volume=float(vol[i]) if i < len(vol) else 0.0,
            )
        )
    return out
