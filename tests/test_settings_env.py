"""Regression: Notepad on Windows saves .env with a UTF-8 BOM. The BOM used
to fuse into the first key name, causing DHAN_ACCESS_TOKEN to be read as
'﻿DHAN_ACCESS_TOKEN' and Settings to see the field as empty.
"""
from __future__ import annotations

import os

import pytest

from app.settings import _load_env_file, load_settings


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EMRB_STATE_DIR", str(tmp_path))
    # .env lives at ~/Documents/shared/.env in production. Point the loader
    # at a tmp_path via the EMRB_ENV_FILE override so these tests remain
    # hermetic and don't read the developer's real credentials file.
    monkeypatch.setenv("EMRB_ENV_FILE", str(tmp_path / ".env"))
    # Clear any inherited values so the test sees only what we write.
    for k in ("DHAN_ACCESS_TOKEN", "DHAN_CLIENT_ID"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _write_env(path, body: bytes):
    p = path / ".env"
    p.write_bytes(body)
    return p


def test_plain_utf8_env_loads(env_dir):
    _write_env(env_dir, b"DHAN_CLIENT_ID=123\nDHAN_ACCESS_TOKEN=abc.def.ghi\n")
    s = load_settings()
    assert s.dhan_client_id == "123"
    assert s.dhan_access_token == "abc.def.ghi"


def test_utf8_bom_is_stripped(env_dir):
    """Notepad on Windows saves with EF BB BF prefix."""
    body = b"\xef\xbb\xbfDHAN_ACCESS_TOKEN=abc.def.ghi\r\nDHAN_CLIENT_ID=123\r\n"
    _write_env(env_dir, body)
    s = load_settings()
    assert s.dhan_access_token == "abc.def.ghi"
    assert s.dhan_client_id == "123"
    # No mangled key in os.environ either.
    assert "﻿DHAN_ACCESS_TOKEN" not in os.environ
    assert "DHAN_ACCESS_TOKEN" in os.environ


def test_crlf_endings_load(env_dir):
    _write_env(env_dir, b"DHAN_ACCESS_TOKEN=abc.def.ghi\r\nDHAN_CLIENT_ID=123\r\n")
    s = load_settings()
    assert s.dhan_access_token == "abc.def.ghi"
    assert s.dhan_client_id == "123"


def test_missing_env_file_is_no_op(env_dir):
    # No .env written
    s = load_settings()
    assert s.dhan_access_token == ""
    assert s.dhan_client_id == ""
