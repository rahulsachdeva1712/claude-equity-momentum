"""Log redaction + structured logging setup. FRD B.4, B.12.

Scrubs JWT access tokens, PINs, TOTP secrets, and any value for a set of
well-known secret field names before the record reaches any handler. Also
installs a JSON formatter so log files are machine-parseable.
"""
from __future__ import annotations

import json
import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Iterable

REDACTED = "***REDACTED***"

# JWT: three base64url segments separated by dots. Require reasonable length
# so we don't redact short random-looking strings.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")

# Bearer tokens in Authorization headers
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._-]+")

# Common secret key names → redact their values whether quoted or not.
_SECRET_KEYS = (
    "access_token",
    "access-token",
    "DHAN_ACCESS_TOKEN",
    "DHAN_PIN",
    "DHAN_TOTP_SECRET",
    "dhan_access_token",
    "dhan_pin",
    "dhan_totp_secret",
    "pin",
    "totp",
    "totp_secret",
    "password",
)

_KV_RE = re.compile(
    r"(?P<key>(?:%s))\s*[=:]\s*(?P<val>\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"
    % "|".join(re.escape(k) for k in _SECRET_KEYS)
)


def redact_text(s: str) -> str:
    s = _JWT_RE.sub(REDACTED, s)
    s = _BEARER_RE.sub(r"\1" + REDACTED, s)
    s = _KV_RE.sub(lambda m: f"{m.group('key')}={REDACTED}", s)
    return s


def redact_mapping(obj):
    """Recursively redact a dict / list. Returns a new structure."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k in _SECRET_KEYS:
                out[k] = REDACTED
            else:
                out[k] = redact_mapping(v)
        return out
    if isinstance(obj, list):
        return [redact_mapping(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            try:
                record.args = tuple(
                    redact_text(a) if isinstance(a, str) else a for a in record.args
                )
            except Exception:  # noqa: BLE001
                pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(log_path: Path, level: str = "INFO", extra_filters: Iterable[logging.Filter] = ()) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(log_path, when="midnight", backupCount=14)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    for f in extra_filters:
        handler.addFilter(f)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    console.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.setLevel(level.upper())
    # Replace handlers so re-configuration during tests is clean.
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(console)
