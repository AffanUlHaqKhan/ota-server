"""OTA server.

Bring-up implementation: real storage, real rollout policy, **no authentication**.
Device routes and operator routes are still separate routers so auth can be
added per-surface without restructuring -- a device credential must never be
able to create a rollout.

    SECURITY: every endpoint here is currently open. That is deliberate for a
    bench loop (DECISIONS.md D5/D6) and must not survive contact with anything
    outside it: an unauthenticated OSTree remote plus an unauthenticated
    control API is arbitrary code execution on every device that trusts it.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import store

app = FastAPI(title="OTA Server", version="0.2.0")

# The Vite dev server runs on a different origin. Tighten before deploying.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

devices = APIRouter(prefix="/api/v1", tags=["device"])
operators = APIRouter(prefix="/api/v1", tags=["operator"])

REPO_PUBLIC_URL = os.environ.get("REPO_PUBLIC_URL", "http://localhost:8080/repo")


# --------------------------------------------------------------------- models
class Heartbeat(BaseModel):
    commit: str
    branch: str
    app_version: str = ""
    healthy: bool = True
    uptime_s: int = 0
    hardware_id: str = ""
    group: str | None = None


class InstallReport(BaseModel):
    commit: str
    result: Literal["success", "download_failed", "deploy_failed",
                    "healthcheck_failed", "rollback"]
    detail: str | None = None


class TargetIn(BaseModel):
    branch: str
    commit: str
    version: str
    size_bytes: int = 0
    delta_bytes: int | None = None
    created_by: str = "ci"


class RolloutIn(BaseModel):
    branch: str
    version: str
    commit: str
    group: str
    strategy: Literal["canary", "staged", "all"] = "canary"
    halt_on_failure_pct: float = 5.0


class PinIn(BaseModel):
    pinned: bool
    reason: str | None = None


# --------------------------------------------------------------- device routes
@devices.post("/devices/{device_id}/heartbeat", status_code=204)
def heartbeat(device_id: str, hb: Heartbeat) -> Response:
    """Upsert. A device that has never been seen registers itself by calling
    this -- fine for a bench loop, unacceptable once devices carry identities
    (DECISIONS.md D6)."""
    c = store.conn()
    row = c.execute("SELECT id FROM device WHERE id=?", (device_id,)).fetchone()
    if row is None:
        c.execute(
            'INSERT INTO device (id, "group", branch, hardware_id, booted_commit,'
            ' app_version, healthy, last_seen, update_state)'
            ' VALUES (?,?,?,?,?,?,?,?,?)',
            (device_id, hb.group or "default", hb.branch, hb.hardware_id,
             hb.commit, hb.app_version, int(hb.healthy), store.now(), "idle"),
        )
        store.log_event(device_id, "register", f"first seen on {hb.branch}")
    else:
        # Booting the pending commit clears it -- the device is now ON it.
        c.execute(
            "UPDATE device SET branch=?, hardware_id=?, booted_commit=?,"
            " app_version=?, healthy=?, last_seen=?,"
            " pending_commit = CASE WHEN pending_commit = ? THEN NULL ELSE pending_commit END,"
            " update_state = CASE WHEN pending_commit = ? THEN 'idle' ELSE update_state END"
            " WHERE id=?",
            (hb.branch, hb.hardware_id, hb.commit, hb.app_version,
             int(hb.healthy), store.now(), hb.commit, hb.commit, device_id),
        )
    c.commit()
    return Response(status_code=204)


@devices.get("/devices/{device_id}/update")
def get_update(device_id: str):
    """The only endpoint with real policy. Every 204 path is a decision -- they
    are logged, because "why is this device not updating" is the most common
    operational question and guessing at it wastes hours."""
    c = store.conn()
    d = c.execute("SELECT * FROM device WHERE id=?", (device_id,)).fetchone()
    if d is None:
        raise HTTPException(404, "unknown device")
    if d["pinned"]:
        return Response(status_code=204, headers={"X-No-Update": "device pinned"})

    r = c.execute(
        'SELECT * FROM rollout WHERE "group"=? AND branch=? AND state=\'running\''
        " ORDER BY created_at DESC LIMIT 1",
        (d["group"], d["branch"]),
    ).fetchone()
    if r is None:
        return Response(status_code=204, headers={"X-No-Update": "no active rollout"})
    if d["booted_commit"] == r["commit_id"]:
        return Response(status_code=204, headers={"X-No-Update": "already on target"})

    t = c.execute("SELECT * FROM target WHERE commit_id=?", (r["commit_id"],)).fetchone()
    c.execute("UPDATE device SET update_state='offered' WHERE id=?", (device_id,))
    c.commit()
    store.log_event(device_id, "offer", f"offered {r['version']}")
    return {
        "commit": r["commit_id"], "branch": r["branch"],
        "repo_url": REPO_PUBLIC_URL,
        "size_bytes": t["size_bytes"] if t else 0,
        "signature": "",   # TODO: real signing, DECISIONS.md D5
    }


@devices.post("/devices/{device_id}/report", status_code=202)
def report(device_id: str, rep: InstallReport):
    c = store.conn()
    d = c.execute("SELECT * FROM device WHERE id=?", (device_id,)).fetchone()
    if d is None:
        raise HTTPException(404, "unknown device")

    if rep.result == "success":
        c.execute(
            "UPDATE device SET booted_commit=?, pending_commit=NULL,"
            " update_state='idle', healthy=1, last_seen=? WHERE id=?",
            (rep.commit, store.now(), device_id),
        )
    else:
        # A rollback pins the device: it must not be re-offered the commit that
        # broke it on the very next poll.
        pin = rep.result == "rollback"
        c.execute(
            "UPDATE device SET update_state='idle', pending_commit=NULL, healthy=0,"
            " pinned=?, pin_reason=?, last_seen=? WHERE id=?",
            (int(pin), f"rolled back from {rep.commit[:10]}" if pin else d["pin_reason"],
             store.now(), device_id),
        )
    c.commit()
    store.log_event(device_id, "report", rep.detail or rep.result, rep.result)
    _maybe_halt(d["group"], d["branch"])
    return {"accepted": True}


def _maybe_halt(group: str, branch: str) -> None:
    """Auto-halt on failure rate. Halting is the safe default: a rollout that
    keeps going through failures is a fleet-wide outage."""
    c = store.conn()
    r = c.execute(
        'SELECT * FROM rollout WHERE "group"=? AND branch=? AND state=\'running\''
        " ORDER BY created_at DESC LIMIT 1", (group, branch)).fetchone()
    if r is None:
        return
    counts = store.rollout_counts(r["id"], group, branch, r["commit_id"])
    attempted = counts["confirmed"] + counts["failed"] + counts["rolled_back"]
    bad = counts["failed"] + counts["rolled_back"]
    if attempted >= 3 and (bad / attempted) * 100 > r["halt_on_failure_pct"]:
        pct = (bad / attempted) * 100
        c.execute(
            "UPDATE rollout SET state='halted', halt_reason=? WHERE id=?",
            (f"failure rate {pct:.1f}% exceeded {r['halt_on_failure_pct']}%", r["id"]),
        )
        c.commit()


# ------------------------------------------------------------- operator routes
@operators.get("/fleet")
def fleet():
    rows = store.conn().execute("SELECT * FROM device ORDER BY id").fetchall()
    return {"devices": [store.device_row(r) for r in rows], "total": len(rows)}


@operators.get("/devices/{device_id}")
def get_device(device_id: str):
    r = store.conn().execute("SELECT * FROM device WHERE id=?", (device_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "unknown device")
    return store.device_row(r)


@operators.get("/devices/{device_id}/events")
def device_events(device_id: str, limit: int = 50):
    rows = store.conn().execute(
        "SELECT * FROM event WHERE device_id=? ORDER BY id DESC LIMIT ?",
        (device_id, limit)).fetchall()
    return [{"ts": r["ts"], "device_id": r["device_id"], "kind": r["kind"],
             "detail": r["detail"], "result": r["result"]} for r in rows]


@operators.post("/devices/{device_id}/pin", status_code=204)
def set_pin(device_id: str, body: PinIn) -> Response:
    store.conn().execute(
        "UPDATE device SET pinned=?, pin_reason=? WHERE id=?",
        (int(body.pinned), body.reason if body.pinned else None, device_id))
    store.conn().commit()
    store.log_event(device_id, "pin", "pinned" if body.pinned else "unpinned")
    return Response(status_code=204)


@operators.get("/targets")
def list_targets(branch: str | None = None):
    q = "SELECT * FROM target"
    args: tuple = ()
    if branch:
        q += " WHERE branch=?"
        args = (branch,)
    q += " ORDER BY created_at DESC"
    return [store.target_row(r) for r in store.conn().execute(q, args).fetchall()]


@operators.post("/targets", status_code=201)
def create_target(t: TargetIn):
    store.conn().execute(
        "INSERT OR REPLACE INTO target"
        " (commit_id, branch, version, size_bytes, delta_bytes, created_at, created_by)"
        " VALUES (?,?,?,?,?,?,?)",
        (t.commit, t.branch, t.version, t.size_bytes, t.delta_bytes,
         store.now(), t.created_by))
    store.conn().commit()
    return store.target_row(
        store.conn().execute("SELECT * FROM target WHERE commit_id=?", (t.commit,)).fetchone())


@operators.get("/rollouts")
def list_rollouts():
    rows = store.conn().execute("SELECT * FROM rollout ORDER BY created_at DESC").fetchall()
    return [store.rollout_row(r) for r in rows]


@operators.get("/rollouts/{rollout_id}")
def get_rollout(rollout_id: str):
    r = store.conn().execute("SELECT * FROM rollout WHERE id=?", (rollout_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "unknown rollout")
    return store.rollout_row(r)


@operators.get("/rollouts/{rollout_id}/devices")
def rollout_devices(rollout_id: str):
    r = store.conn().execute("SELECT * FROM rollout WHERE id=?", (rollout_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "unknown rollout")
    rows = store.conn().execute(
        'SELECT * FROM device WHERE "group"=? AND branch=? ORDER BY id',
        (r["group"], r["branch"])).fetchall()
    return [store.device_row(x) for x in rows]


@operators.post("/rollouts", status_code=201)
def create_rollout(body: RolloutIn):
    rid = f"rollout-{uuid.uuid4().hex[:8]}"
    waves = [5, 25, 100] if body.strategy == "staged" else [100]
    store.conn().execute(
        'INSERT INTO rollout (id, branch, version, commit_id, "group", strategy,'
        " state, waves, current_wave, halt_on_failure_pct, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rid, body.branch, body.version, body.commit, body.group, body.strategy,
         "running", json.dumps(waves), 0, body.halt_on_failure_pct, store.now()))
    store.conn().commit()
    return get_rollout(rid)


@operators.post("/rollouts/{rollout_id}/pause")
def pause_rollout(rollout_id: str):
    store.conn().execute("UPDATE rollout SET state='paused' WHERE id=?", (rollout_id,))
    store.conn().commit()
    return get_rollout(rollout_id)


@operators.post("/rollouts/{rollout_id}/resume")
def resume_rollout(rollout_id: str):
    store.conn().execute(
        "UPDATE rollout SET state='running', halt_reason=NULL WHERE id=?", (rollout_id,))
    store.conn().commit()
    return get_rollout(rollout_id)


app.include_router(devices)
app.include_router(operators)


@app.on_event("startup")
def _startup() -> None:
    store.init()


@app.get("/healthz")
def healthz():
    return {"ok": True}
