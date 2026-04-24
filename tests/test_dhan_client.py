"""Dhan client tests with respx-mocked HTTP."""
from __future__ import annotations

import base64
import hmac
import hashlib
import json
import time

import httpx
import pytest
import respx

from app.dhan.client import DhanClient, jwt_expiry_epoch, jwt_seconds_to_expiry
from app.dhan.errors import DhanAuthError, DhanError, DhanRejected, DhanUnavailable
from app.dhan.models import PlaceOrderRequest


BASE = "https://api.dhan.example/v2"


def _make_jwt(exp_in: int = 3600) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"typ": "JWT", "alg": "HS512"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + exp_in}).encode()).rstrip(b"=")
    signing_input = header + b"." + payload
    sig = hmac.new(b"k", signing_input, hashlib.sha512).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
    return (signing_input + b"." + sig_b64).decode()


def test_jwt_expiry_parsing():
    t = _make_jwt(exp_in=3600)
    exp = jwt_expiry_epoch(t)
    assert exp is not None
    assert jwt_seconds_to_expiry(t) > 0


def test_jwt_bad_token_returns_none():
    assert jwt_expiry_epoch("not-a-jwt") is None
    assert jwt_seconds_to_expiry("not-a-jwt") is None


@pytest.mark.asyncio
async def test_validate_token_true():
    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/fundlimit").mock(return_value=httpx.Response(200, json={"availableBalance": 100000}))
        c = DhanClient(BASE, "cid", "tok")
        assert await c.validate_token() is True
        await c.close()


@pytest.mark.asyncio
async def test_validate_token_401():
    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/fundlimit").mock(return_value=httpx.Response(401, json={"error": "bad token"}))
        c = DhanClient(BASE, "cid", "tok")
        assert await c.validate_token() is False
        await c.close()


@pytest.mark.asyncio
async def test_market_status_uses_ist_clock_fallback(monkeypatch):
    """Dhan retired /marketfeed/marketstatus; market_status now derives
    OPEN/CLOSED from a local IST weekday + market-hours check. FRD B.5."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    c = DhanClient(BASE, "cid", "tok")

    # Wednesday 10:00 IST — market open.
    monkeypatch.setattr("app.time_utils.now_ist", lambda: datetime(2026, 4, 22, 10, 0, tzinfo=ist))
    assert await c.market_status() == "OPEN"

    # Wednesday 18:00 IST — after-hours.
    monkeypatch.setattr("app.time_utils.now_ist", lambda: datetime(2026, 4, 22, 18, 0, tzinfo=ist))
    assert await c.market_status() == "CLOSED"

    # Saturday 10:00 IST — weekend.
    monkeypatch.setattr("app.time_utils.now_ist", lambda: datetime(2026, 4, 25, 10, 0, tzinfo=ist))
    assert await c.market_status() == "CLOSED"

    await c.close()


@pytest.mark.asyncio
async def test_place_order_returns_id_and_rejects_raise():
    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        req = PlaceOrderRequest(
            security_id="123",
            exchange_segment="BSE_EQ",
            transaction_type="BUY",
            quantity=10,
            correlation_id="emrb:x",
        )
        m.post("/orders").mock(return_value=httpx.Response(200, json={"orderId": "ORD1"}))
        c = DhanClient(BASE, "cid", "tok")
        oid = await c.place_order(req)
        assert oid == "ORD1"

        m.post("/orders").mock(return_value=httpx.Response(200, json={"orderStatus": "REJECTED", "remarks": "no margin"}))
        with pytest.raises(DhanRejected):
            await c.place_order(req)
        await c.close()


@pytest.mark.asyncio
async def test_server_5xx_raises_unavailable():
    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/positions").mock(return_value=httpx.Response(503, text="sorry"))
        c = DhanClient(BASE, "cid", "tok")
        with pytest.raises(DhanUnavailable):
            await c.positions()
        await c.close()


@pytest.mark.asyncio
async def test_client_4xx_raises_dhan_error():
    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/positions").mock(return_value=httpx.Response(400, json={"error": "bad"}))
        c = DhanClient(BASE, "cid", "tok")
        with pytest.raises(DhanError):
            await c.positions()
        await c.close()


@pytest.mark.asyncio
async def test_candles_parsed_from_column_oriented_response():
    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.post("/charts/historical").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "timestamp": [1712000000, 1712086400],
                        "open": [100.0, 101.0],
                        "high": [101.0, 102.0],
                        "low": [99.0, 100.5],
                        "close": [100.5, 101.5],
                        "volume": [10000, 12000],
                    }
                },
            )
        )
        c = DhanClient(BASE, "cid", "tok")
        bars = await c.historical_daily("123", "BSE_EQ", "2024-04-01", "2024-04-02")
        assert len(bars) == 2
        assert bars[0].close == 100.5
        await c.close()


@pytest.mark.asyncio
async def test_positions_parsed_and_pass_raw():
    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/positions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "tradingSymbol": "CDG",
                            "securityId": "1",
                            "exchangeSegment": "BSE_EQ",
                            "netQty": 140,
                            "buyAvg": 143.69,
                            "ltp": 158.20,
                            "unrealizedProfit": 2030.9,
                            "realizedProfit": 0.0,
                        }
                    ]
                },
            )
        )
        c = DhanClient(BASE, "cid", "tok")
        ps = await c.positions()
        assert len(ps) == 1
        assert ps[0].symbol == "CDG"
        assert ps[0].net_qty == 140
        assert ps[0].raw["tradingSymbol"] == "CDG"
        await c.close()


@pytest.mark.asyncio
async def test_auth_header_is_sent():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    async with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/fundlimit").mock(side_effect=handler)
        c = DhanClient(BASE, "client123", "abc.def.ghi")
        await c.validate_token()
        await c.close()

    assert captured.get("access-token") == "abc.def.ghi"
    assert captured.get("client-id") == "client123"
