"""SQLite persistence for the OTA server.

Deliberately stdlib sqlite3 rather than an ORM: the schema is small, the
queries are simple, and a bring-up server should have as few moving parts as
possible. Swap for SQLAlchemy + postgres when the fleet outgrows one file.

The `events` table is append-only on purpose. When a rollout goes wrong the
postmortem needs the ordering of what each device reported, not the current
state.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

DB_PATH = os.environ.get("OTA_DB_PATH", "ota.db")
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
    id              TEXT PRIMARY KEY,
    "group"         TEXT NOT NULL DEFAULT 'default',
    branch          TEXT NOT NULL DEFAULT '',
    hardware_id     TEXT NOT NULL DEFAULT '',
    booted_commit   TEXT NOT NULL DEFAULT '',
    pending_commit  TEXT,
    app_version     TEXT NOT NULL DEFAULT '',
    healthy         INTEGER NOT NULL DEFAULT 1,
    last_seen       TEXT NOT NULL,
    update_state    TEXT NOT NULL DEFAULT 'idle',
    pinned          INTEGER NOT NULL DEFAULT 0,
    pin_reason      TEXT
);

CREATE TABLE IF NOT EXISTS target (
    commit_id   TEXT PRIMARY KEY,
    branch      TEXT NOT NULL,
    version     TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    delta_bytes INTEGER,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL DEFAULT 'ci'
);

CREATE TABLE IF NOT EXISTS rollout (
    id                  TEXT PRIMARY KEY,
    branch              TEXT NOT NULL,
    version             TEXT NOT NULL,
    commit_id           TEXT NOT NULL,
    "group"             TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    state               TEXT NOT NULL,
    waves               TEXT NOT NULL DEFAULT '[100]',
    current_wave        INTEGER NOT NULL DEFAULT 0,
    halt_on_failure_pct REAL NOT NULL DEFAULT 5,
    created_at          TEXT NOT NULL,
    halt_reason         TEXT
);

CREATE TABLE IF NOT EXISTS event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    device_id TEXT NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    result    TEXT
);
CREATE INDEX IF NOT EXISTS event_device_ts ON event(device_id, id DESC);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init() -> None:
    conn().executescript(SCHEMA)
    conn().commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(device_id: str, kind: str, detail: str = "", result: str | None = None) -> None:
    conn().execute(
        "INSERT INTO event (ts, device_id, kind, detail, result) VALUES (?,?,?,?,?)",
        (now(), device_id, kind, detail, result),
    )
    conn().commit()


def device_row(r: sqlite3.Row) -> dict[str, Any]:
    """Shape a row the way ota-web-ui's Device type expects."""
    return {
        "id": r["id"],
        "group": r["group"],
        "branch": r["branch"],
        "hardware_id": r["hardware_id"],
        "booted_commit": r["booted_commit"],
        "pending_commit": r["pending_commit"],
        "app_version": r["app_version"],
        "healthy": bool(r["healthy"]),
        "last_seen": r["last_seen"],
        "update_state": r["update_state"],
        "pinned": bool(r["pinned"]),
        "pin_reason": r["pin_reason"],
    }


def target_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "branch": r["branch"], "commit": r["commit_id"], "version": r["version"],
        "size_bytes": r["size_bytes"], "delta_bytes": r["delta_bytes"],
        "created_at": r["created_at"], "created_by": r["created_by"],
    }


def rollout_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"], "branch": r["branch"], "version": r["version"],
        "commit": r["commit_id"], "group": r["group"], "strategy": r["strategy"],
        "state": r["state"], "waves": json.loads(r["waves"]),
        "current_wave": r["current_wave"],
        "halt_on_failure_pct": r["halt_on_failure_pct"],
        "created_at": r["created_at"], "halt_reason": r["halt_reason"],
        "counts": rollout_counts(r["id"], r["group"], r["branch"], r["commit_id"]),
    }


def rollout_counts(rollout_id: str, group: str, branch: str, commit: str) -> dict[str, int]:
    """Derived from device state rather than stored, so the numbers cannot drift
    away from reality. Cheap at this scale; precompute if the fleet grows."""
    rows = conn().execute(
        'SELECT * FROM device WHERE "group"=? AND branch=?', (group, branch)
    ).fetchall()
    c = dict(waiting=0, offered=0, downloading=0, pending_reboot=0,
             confirmed=0, failed=0, rolled_back=0)
    for r in rows:
        if r["booted_commit"] == commit:
            c["confirmed"] += 1
        elif r["pinned"] and (r["pin_reason"] or "").startswith("rolled back"):
            c["rolled_back"] += 1
        elif r["update_state"] in ("offered", "downloading", "pending_reboot"):
            c[r["update_state"]] += 1
        else:
            c["waiting"] += 1
    return c
