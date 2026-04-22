from app.redaction import REDACTED, redact_mapping, redact_text


JWT = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwiZXhwIjoxNzc2Nzgz"
    "NDE0fQ.SignatureABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"
)


def test_jwt_is_redacted():
    s = f"access_token={JWT} client=123"
    out = redact_text(s)
    assert JWT not in out
    assert REDACTED in out


def test_bearer_header_redacted():
    s = "Authorization: Bearer abcd.ef.gh"
    out = redact_text(s)
    assert "Bearer " + REDACTED in out


def test_key_value_variants_redacted():
    for form in (
        'DHAN_ACCESS_TOKEN="secret_value_123"',
        "DHAN_PIN=145464",
        "dhan_totp_secret='TAYULGVOC4PF6YOZ'",
        "password: hunter2",
    ):
        assert "secret_value_123" not in redact_text(form)
        assert "145464" not in redact_text("DHAN_PIN=145464")
        assert "TAYULGVOC4PF6YOZ" not in redact_text(form)
        assert "hunter2" not in redact_text(form)


def test_redact_mapping_recursive():
    payload = {
        "dhan_client_id": "1102613695",
        "DHAN_ACCESS_TOKEN": JWT,
        "nested": {"password": "x", "ok": "value"},
        "list": [{"access_token": JWT}, "plain"],
    }
    out = redact_mapping(payload)
    assert out["DHAN_ACCESS_TOKEN"] == REDACTED
    assert out["nested"]["password"] == REDACTED
    assert out["nested"]["ok"] == "value"
    assert out["list"][0]["access_token"] == REDACTED
    assert out["list"][1] == "plain"
    assert out["dhan_client_id"] == "1102613695"
