"""Alert helpers. FRD B.11."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.time_utils import now_ist

Severity = Literal["info", "warn", "error", "critical"]


@dataclass
class Alert:
    severity: Severity
    source: str
    message: str
    payload: dict = field(default_factory=dict)


def raise_alert(conn: sqlite3.Connection, alert: Alert) -> int:
    cur = conn.execute(
        "INSERT INTO alerts (severity, source, message, payload_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            alert.severity,
            alert.source,
            alert.message,
            json.dumps(alert.payload, default=str),
            now_ist().isoformat(),
        ),
    )
    return int(cur.lastrowid)


def list_alerts(conn: sqlite3.Connection, only_unacked: bool = True, limit: int = 100) -> list[dict]:
    q = "SELECT id, severity, source, message, payload_json, created_at, acknowledged_at FROM alerts"
    if only_unacked:
        q += " WHERE acknowledged_at IS NULL"
    q += " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(q, (limit,))]


def acknowledge(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute(
        "UPDATE alerts SET acknowledged_at = ? WHERE id = ? AND acknowledged_at IS NULL",
        (now_ist().isoformat(), alert_id),
    )
