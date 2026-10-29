"""Backups. Administrators manage destinations and run backups; owners of a
website can back it up and restore it."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import auth
from ..deps import AdminPrincipal, Agent, CurrentPrincipal, Db, Principal, client_ip
from .common import call_agent
from .websites import _authorize as authorize_site
from .websites import _normalize, _owners

router = APIRouter(prefix="/api/backups", tags=["backups"])


class Schedule(BaseModel):
    frequency: str = Field(default="daily", pattern="^(daily|weekly|manual)$")
    time: str = Field(default="03:00", max_length=5)
    weekday: int = Field(default=6, ge=0, le=6)


class Keep(BaseModel):
    daily: int = Field(default=7, ge=0, le=366)
    weekly: int = Field(default=4, ge=0, le=260)
    monthly: int = Field(default=6, ge=0, le=240)


class Include(BaseModel):
    files: bool = True
    databases: bool = True
    mail: bool = True
    server: bool = True


class DestinationBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    type: str = Field(pattern="^(local|s3|b2|sftp)$")
    path: str | None = Field(default=None, max_length=200)
    endpoint: str | None = Field(default=None, max_length=200)
    bucket: str | None = Field(default=None, max_length=63)
    prefix: str | None = Field(default=None, max_length=200)
    region: str | None = Field(default=None, max_length=30)
    access_key: str | None = Field(default=None, max_length=128)
    secret_key: str | None = Field(default=None, max_length=200)
    account_id: str | None = Field(default=None, max_length=128)
    account_key: str | None = Field(default=None, max_length=200)
    host: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    user: str | None = Field(default=None, max_length=32)
    repo_password: str | None = Field(default=None, max_length=200)
    schedule: Schedule = Schedule()
    keep: Keep = Keep()
    include: Include = Include()
    notify_email: str | None = Field(default=None, max_length=254)


class DestinationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    access_key: str | None = Field(default=None, max_length=128)
    secret_key: str | None = Field(default=None, max_length=200)
    account_id: str | None = Field(default=None, max_length=128)
    account_key: str | None = Field(default=None, max_length=200)
    schedule: Schedule | None = None
    keep: Keep | None = None
    include: Include | None = None
    notify_email: str | None = Field(default=None, max_length=254)


class RunBody(BaseModel):
    destination: str | None = Field(default=None, max_length=16)


class RestoreBody(BaseModel):
    destination: str = Field(max_length=16)
    snapshot: str = Field(min_length=8, max_length=64)
    files: bool = False
    databases: bool = False
    mail: bool = False
    path: str | None = Field(default=None, max_length=500)


def _audit(db: sqlite3.Connection, request: Request, principal: Principal, event: str,
           detail: str) -> None:  # fmt: skip
    auth.audit(db, event, user_id=principal.user.id, ip=client_ip(request), detail=detail)


def _own_domains(db: sqlite3.Connection, principal: Principal) -> set[str]:
    return {d for d, meta in _owners(db).items()
            if meta["owner"] and meta["owner"]["id"] == principal.user.id}  # fmt: skip


def _may_see(job: dict[str, Any], principal: Principal, own: set[str]) -> bool:
    if principal.user.role == "admin":
        return True
    if job["kind"] == "restore":
        return job.get("domain") in own
    domains = job.get("domains") or []
    return bool(domains) and set(domains) <= own


@router.get("/status")
def backup_status(principal: CurrentPrincipal, agent: Agent):
    data = call_agent(agent, "backup.status")
    if principal.user.role == "admin":
        return data
    return {
        "restic_installed": data["restic_installed"],
        "destinations": [{"id": d["id"], "name": d["name"]} for d in data["destinations"]],
    }


@router.post("/ssh-key")
def ssh_key(_admin: AdminPrincipal, agent: Agent):
    return call_agent(agent, "backup.ssh_key")


@router.post("/destinations", status_code=status.HTTP_201_CREATED)
def create_destination(body: DestinationBody, request: Request, admin: AdminPrincipal, db: Db,
                       agent: Agent):  # fmt: skip
    params = body.model_dump(exclude_none=True)
    result = call_agent(agent, "backup.destination.create", params, timeout=180)
    _audit(db, request, admin, "backup.destination.create", f"{result['name']} ({result['type']})")
    return result


@router.put("/destinations/{dest_id}")
def update_destination(dest_id: str, body: DestinationUpdate, request: Request,
                       admin: AdminPrincipal, db: Db, agent: Agent):  # fmt: skip
    params = {"id": dest_id, **body.model_dump(exclude_none=True)}
    result = call_agent(agent, "backup.destination.update", params, timeout=180)
    _audit(db, request, admin, "backup.destination.update", result["name"])
    return result


@router.delete("/destinations/{dest_id}")
def delete_destination(dest_id: str, request: Request, admin: AdminPrincipal, db: Db,
                       agent: Agent):  # fmt: skip
    result = call_agent(agent, "backup.destination.delete", {"id": dest_id})
    _audit(db, request, admin, "backup.destination.delete", result["name"])
    return result


@router.post("/destinations/{dest_id}/password")
def destination_password(dest_id: str, request: Request, admin: AdminPrincipal, db: Db,
                         agent: Agent):  # fmt: skip
    result = call_agent(agent, "backup.destination.password", {"id": dest_id})
    _audit(db, request, admin, "backup.destination.password", dest_id)
    return result


@router.get("/destinations/{dest_id}/usage")
def destination_usage(dest_id: str, _admin: AdminPrincipal, agent: Agent):
    return call_agent(agent, "backup.destination.usage", {"id": dest_id}, timeout=300)


@router.post("/destinations/{dest_id}/run", status_code=status.HTTP_202_ACCEPTED)
def run_destination(dest_id: str, request: Request, admin: AdminPrincipal, db: Db, agent: Agent):
    job = call_agent(agent, "backup.run", {"destination": dest_id})
    _audit(db, request, admin, "backup.run", dest_id)
    return job


@router.get("/jobs")
def list_jobs(principal: CurrentPrincipal, db: Db, agent: Agent):
    own = _own_domains(db, principal)
    jobs = call_agent(agent, "backup.jobs", {"limit": 50})
    jobs = [j for j in jobs if j["kind"] in ("backup", "restore")]
    return {"jobs": [j for j in jobs if _may_see(j, principal, own)][:30]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, principal: CurrentPrincipal, db: Db, agent: Agent):
    job = call_agent(agent, "backup.job.get", {"id": job_id})
    if not _may_see(job, principal, _own_domains(db, principal)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
    return job


@router.get("/sites/{domain}/snapshots")
def site_snapshots(domain: str, principal: CurrentPrincipal, db: Db, agent: Agent):
    domain = _normalize(domain)
    authorize_site(db, principal, domain)
    return call_agent(agent, "backup.snapshots", {"domain": domain}, timeout=300)


@router.post("/sites/{domain}/run", status_code=status.HTTP_202_ACCEPTED)
def run_site(domain: str, body: RunBody, request: Request, principal: CurrentPrincipal, db: Db,
             agent: Agent):  # fmt: skip
    domain = _normalize(domain)
    authorize_site(db, principal, domain)
    dest = body.destination
    if not dest:
        dests = call_agent(agent, "backup.status")["destinations"]
        if not dests:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no backup destination is set up")
        dest = dests[0]["id"]
    job = call_agent(agent, "backup.run", {"destination": dest, "domains": [domain]})
    _audit(db, request, principal, "backup.run", domain)
    return job


@router.post("/sites/{domain}/restore", status_code=status.HTTP_202_ACCEPTED)
def restore_site(domain: str, body: RestoreBody, request: Request, principal: CurrentPrincipal,
                 db: Db, agent: Agent):  # fmt: skip
    domain = _normalize(domain)
    authorize_site(db, principal, domain)
    params = {
        "domain": domain,
        "destination": body.destination,
        "snapshot": body.snapshot,
        "parts": {"files": body.files, "databases": body.databases, "mail": body.mail},
        "path": body.path or "",
    }
    job = call_agent(agent, "backup.restore", params)
    parts = ", ".join(k for k in ("files", "databases", "mail") if params["parts"][k])
    detail = f"{domain} from {body.snapshot[:8]}: {parts}"
    if body.path:
        detail += f" ({body.path})"
    _audit(db, request, principal, "backup.restore", detail)
    return job
