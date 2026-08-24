"""OTA server API skeleton.

Device routes and operator routes are deliberately separate routers with separate auth:
a device credential must never be able to create or modify a rollout.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel

app = FastAPI(title="OTA Server", version="0.1.0")

devices = APIRouter(prefix="/api/v1", tags=["device"])
operators = APIRouter(prefix="/api/v1", tags=["operator"])


# --------------------------------------------------------------------------- models
class Heartbeat(BaseModel):
    commit: str                      # currently booted ostree commit
    branch: str
    app_version: str
    healthy: bool
    uptime_s: int


class UpdateOffer(BaseModel):
    commit: str
    branch: str
    repo_url: str
    size_bytes: int
    signature: str                   # detached signature over the target metadata


class InstallReport(BaseModel):
    commit: str
    result: Literal["success", "download_failed", "deploy_failed", "healthcheck_failed", "rollback"]
    detail: str | None = None


class TargetIn(BaseModel):
    branch: str
    commit: str
    version: str


# --------------------------------------------------------------------------- auth stubs
def device_identity() -> str:
    """Extract device id from the mTLS client cert (nginx passes it in a header)."""
    raise NotImplementedError("wire to X-SSL-Client-S-DN / verified client cert")


def operator_identity() -> str:
    """Validate the operator's OIDC/JWT bearer token."""
    raise NotImplementedError("wire to your IdP")


# --------------------------------------------------------------------------- device routes
@devices.post("/devices/{device_id}/heartbeat", status_code=204)
def heartbeat(device_id: str, hb: Heartbeat, _: str = Depends(device_identity)) -> Response:
    # persist: last_seen, commit, app_version, health
    # a health flip to False during an active rollout should count toward the halt threshold
    return Response(status_code=204)


@devices.get("/devices/{device_id}/update", response_model=UpdateOffer | None)
def get_update(device_id: str, _: str = Depends(device_identity)):
    """204 when nothing to do. This is the only place rollout policy is applied."""
    # 1. look up device -> group -> active rollout
    # 2. is the device in the current wave? inside its maintenance window? not pinned?
    # 3. return the signed target, else 204
    return Response(status_code=204)


@devices.post("/devices/{device_id}/report", status_code=202)
def report(device_id: str, r: InstallReport, _: str = Depends(device_identity)):
    # a "rollback" or repeated failures must auto-pause the rollout
    return {"accepted_at": datetime.now(timezone.utc)}


# --------------------------------------------------------------------------- operator routes
@operators.post("/targets", status_code=201)
def create_target(t: TargetIn, _: str = Depends(operator_identity)):
    """Called by CI after `push-ostree.sh` uploads objects. Signs and records the target."""
    return {"branch": t.branch, "commit": t.commit, "version": t.version}


@operators.get("/targets")
def list_targets(branch: str | None = None, _: str = Depends(operator_identity)):
    return []


@operators.post("/rollouts", status_code=201)
def create_rollout(_: str = Depends(operator_identity)):
    return {"id": "rollout-0", "state": "pending"}


@operators.post("/rollouts/{rollout_id}/pause")
def pause_rollout(rollout_id: str, _: str = Depends(operator_identity)):
    return {"id": rollout_id, "state": "paused"}


@operators.get("/fleet")
def fleet(_: str = Depends(operator_identity)):
    return {"devices": [], "total": 0}


app.include_router(devices)
app.include_router(operators)


@app.get("/healthz")
def healthz():
    return {"ok": True}
