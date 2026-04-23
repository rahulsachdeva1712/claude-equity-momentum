"""Top-bar token classifier. Distinguishes missing, invalid, expiring, valid."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.web.views import _classify_token


def _make_jwt(exp_in: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"typ": "JWT", "alg": "HS512"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + exp_in}).encode()).rstrip(b"=")
    sign_input = header + b"." + payload
    sig = hmac.new(b"k", sign_input, hashlib.sha512).digest()
    return (sign_input + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()


def test_missing_token_shows_no_token():
    state, label = _classify_token("")
    assert state == "missing"
    assert "no token" in label


def test_unparseable_token_shows_invalid_with_hint():
    state, label = _classify_token("not-a-jwt-at-all")
    assert state == "invalid"
    assert "BOM" in label or "quote" in label.lower()


def test_bom_prefixed_value_is_invalid():
    """If the BOM survived into the value (e.g. .env wasn't bom-stripped),
    classifier should still flag as invalid rather than crash."""
    state, _ = _classify_token("﻿eyJsomething.bad")
    assert state == "invalid"


def test_expired_token():
    state, label = _classify_token(_make_jwt(exp_in=-10))
    assert state == "expired"
    assert label == "expired"


def test_expiring_token_within_an_hour():
    state, label = _classify_token(_make_jwt(exp_in=600))
    assert state == "expiring"
    assert "min" in label


def test_valid_token_shows_h_m():
    state, label = _classify_token(_make_jwt(exp_in=4 * 3600 + 22 * 60))
    assert state == "valid"
    assert "4h" in label
    assert "22m" in label
