"""Tiny SQLite persistence layer (single file on a Fly volume)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,            -- resy | sevenrooms | opentable
    mode TEXT NOT NULL,                -- snipe | monitor | reminder
    status TEXT NOT NULL,              -- scheduled | monitoring | attempting | booked | found | failed | cancelled | expired | paused
    venue_id TEXT NOT NULL,
    venue_name TEXT NOT NULL,
    venue_slug TEXT,
    venue_city TEXT,
    venue_tz TEXT,
    venue_url TEXT,
    party_size INTEGER NOT NULL,
    dates TEXT NOT NULL,               -- JSON list of YYYY-MM-DD
    time_start TEXT NOT NULL,          -- HH:MM earliest acceptable
    time_end TEXT NOT NULL,            -- HH:MM latest acceptable
    ideal_time TEXT,                   -- HH:MM preferred (ranking)
    seating_exclude TEXT,              -- JSON list of substrings to skip (e.g. "bar", "patio")
    auto_book INTEGER NOT NULL DEFAULT 1,
    release_at TEXT,                   -- ISO UTC: when reservations open (snipe/reminder)
    snipe_window_seconds INTEGER,
    fallback_to_monitor INTEGER NOT NULL DEFAULT 1,
    poll_interval_seconds INTEGER,
    monitor_until TEXT,                -- ISO UTC
    next_check_at TEXT,                -- ISO UTC (monitor scheduling)
    booked_date TEXT,
    booked_time TEXT,
    booked_seating TEXT,
    confirmation TEXT,
    booking_token TEXT,                -- resy_token, needed to cancel
    found_link TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_checked TEXT,
    last_error TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_req ON events(request_id, id);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    platform TEXT NOT NULL,
    kind TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, platform, kind)
);
"""

JSON_FIELDS = {"dates", "seating_exclude"}
BOOL_FIELDS = {"auto_book", "fallback_to_monitor"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(settings.data_dir, exist_ok=True)
            _conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        return _conn


def reset_for_tests(path: str) -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        settings.data_dir = path


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS:
        if k in d:
            d[k] = json.loads(d[k]) if d[k] else []
    for k in BOOL_FIELDS:
        if k in d and d[k] is not None:
            d[k] = bool(d[k])
    return d


def _encode(values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in values.items():
        if k in JSON_FIELDS:
            v = json.dumps(v or [])
        elif k in BOOL_FIELDS and v is not None:
            v = 1 if v else 0
        out[k] = v
    return out


# ---------------------------------------------------------------- requests

def create_request(values: dict[str, Any]) -> dict[str, Any]:
    rid = uuid.uuid4().hex[:12]
    ts = now_iso()
    row = _encode({**values, "id": rid, "created_at": ts, "updated_at": ts})
    cols = ",".join(row)
    qs = ",".join("?" for _ in row)
    with _lock:
        conn().execute(f"INSERT INTO requests ({cols}) VALUES ({qs})", list(row.values()))
    return get_request(rid)  # type: ignore[return-value]


def get_request(rid: str) -> dict[str, Any] | None:
    with _lock:
        return _decode(conn().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())


def list_requests(statuses: list[str] | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM requests"
    args: list[Any] = []
    if statuses:
        sql += f" WHERE status IN ({','.join('?' for _ in statuses)})"
        args = list(statuses)
    sql += " ORDER BY created_at DESC"
    with _lock:
        return [_decode(r) for r in conn().execute(sql, args).fetchall()]  # type: ignore[misc]


def update_request(rid: str, **values: Any) -> dict[str, Any] | None:
    if not values:
        return get_request(rid)
    values["updated_at"] = now_iso()
    row = _encode(values)
    sets = ",".join(f"{k}=?" for k in row)
    with _lock:
        conn().execute(f"UPDATE requests SET {sets} WHERE id=?", [*row.values(), rid])
    return get_request(rid)


def delete_request(rid: str) -> None:
    with _lock:
        conn().execute("DELETE FROM requests WHERE id=?", (rid,))
        conn().execute("DELETE FROM events WHERE request_id=?", (rid,))


# ------------------------------------------------------------------ events

def log_event(request_id: str | None, message: str, level: str = "info") -> None:
    with _lock:
        conn().execute(
            "INSERT INTO events (request_id, ts, level, message) VALUES (?,?,?,?)",
            (request_id, now_iso(), level, message[:2000]),
        )


def list_events(request_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        if request_id:
            rows = conn().execute(
                "SELECT * FROM events WHERE request_id=? ORDER BY id DESC LIMIT ?", (request_id, limit)
            ).fetchall()
        else:
            rows = conn().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- kv

def kv_get(key: str, default: Any = None) -> Any:
    with _lock:
        row = conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(key: str, value: Any) -> None:
    with _lock:
        conn().execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def kv_delete(key: str) -> None:
    with _lock:
        conn().execute("DELETE FROM kv WHERE key=?", (key,))


# ------------------------------------------------------------------- usage

def today_key(tz_name: str | None = None) -> str:
    """Usage counters reset at local midnight (APP_TIMEZONE)."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz_name or settings.app_timezone)).strftime("%Y-%m-%d")


def usage_incr(platform: str, kind: str = "call", n: int = 1) -> int:
    day = today_key()
    with _lock:
        conn().execute(
            "INSERT INTO usage (day, platform, kind, count) VALUES (?,?,?,?) "
            "ON CONFLICT(day, platform, kind) DO UPDATE SET count=count+excluded.count",
            (day, platform, kind, n),
        )
        row = conn().execute(
            "SELECT count FROM usage WHERE day=? AND platform=? AND kind=?", (day, platform, kind)
        ).fetchone()
    return int(row["count"]) if row else 0


def usage_get(platform: str, kind: str = "call") -> int:
    with _lock:
        row = conn().execute(
            "SELECT count FROM usage WHERE day=? AND platform=? AND kind=?", (today_key(), platform, kind)
        ).fetchone()
    return int(row["count"]) if row else 0
