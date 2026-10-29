"""WordPress. Whoever manages a website can install WordPress on it, update it,
make a staging copy and push that copy live."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import auth
from ..deps import Agent, CurrentPrincipal, Db, Principal, client_ip
from .common import call_agent
from .websites import _authorize, _normalize, _owners, _visible

router = APIRouter(prefix="/api/wordpress", tags=["wordpress"])


class InstallBody(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    admin_user: str = Field(min_length=3, max_length=60)
    admin_email: str = Field(max_length=254)
    password: str = Field(min_length=10, max_length=128)
    locale: str = Field(default="en_US", max_length=12)


class UpdateBody(BaseModel):
    core: bool = True
    plugins: bool = True
    themes: bool = True


class PushBody(BaseModel):
    files: bool = True
    database: bool = True


class CloneBody(BaseModel):
    target: str = Field(min_length=1, max_length=253)


def _audit(
    db: sqlite3.Connection, request: Request, principal: Principal, event: str, detail: str
) -> None:
    auth.audit(db, event, user_id=principal.user.id, ip=client_ip(request), detail=detail)


def _create_site(db: sqlite3.Connection, agent: Agent, domain: str, source: str) -> dict[str, Any]:
    """A new website with the same PHP version and owner as `source`."""
    php = call_agent(agent, "website.get", {"domain": source})["php_version"]
    site = call_agent(
        agent, "website.create", {"domain": domain, "php_version": php, "include_www": False}
    )
    meta = _owners(db).get(source)
    owner_id = meta["owner"]["id"] if meta and meta["owner"] else None
    db.execute(
        "INSERT INTO websites (domain, owner_id, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(domain) DO UPDATE SET owner_id = excluded.owner_id",
        (site["domain"], owner_id, int(time.time())),
    )
    return site


def _remove_site(db: sqlite3.Connection, agent: Agent, domain: str) -> None:
    try:
        call_agent(agent, "website.delete", {"domain": domain, "remove_files": True})
    finally:
        db.execute("DELETE FROM websites WHERE domain = ?", (domain,))


def _visible_domains(db: sqlite3.Connection, principal: Principal) -> set[str]:
    return {d for d, meta in _owners(db).items() if _visible(principal, meta)}


@router.get("/sites")
def list_sites(principal: CurrentPrincipal, db: Db, agent: Agent):
    visible = _visible_domains(db, principal)
    admin = principal.user.role == "admin"
    sites = call_agent(agent, "wordpress.list")
    return {"sites": [s for s in sites if admin or s["domain"] in visible]}


@router.get("/sites/{domain}")
def site_info(domain: str, principal: CurrentPrincipal, db: Db, agent: Agent):
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    return call_agent(agent, "wordpress.info", {"domain": domain}, timeout=300)


@router.post("/sites/{domain}/install", status_code=status.HTTP_202_ACCEPTED)
def install(
    domain: str,
    body: InstallBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Db,
    agent: Agent,
):
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    job = call_agent(agent, "wordpress.install", {"domain": domain, **body.model_dump()})
    _audit(db, request, principal, "wordpress.install", domain)
    return job


@router.post("/sites/{domain}/update", status_code=status.HTTP_202_ACCEPTED)
def update(
    domain: str,
    body: UpdateBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Db,
    agent: Agent,
):
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    job = call_agent(agent, "wordpress.update", {"domain": domain, **body.model_dump()})
    _audit(db, request, principal, "wordpress.update", domain)
    return job


@router.post("/sites/{domain}/staging", status_code=status.HTTP_202_ACCEPTED)
def create_staging(
    domain: str, request: Request, principal: CurrentPrincipal, db: Db, agent: Agent
):
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    staging = f"staging.{domain}"
    if _owners(db).get(staging):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{staging} already exists")
    _create_site(db, agent, staging, domain)
    try:
        job = call_agent(
            agent, "wordpress.clone", {"domain": domain, "target": staging, "mode": "staging"}
        )
    except HTTPException:
        _remove_site(db, agent, staging)
        raise
    _audit(db, request, principal, "wordpress.staging", f"{domain} → {staging}")
    return job


@router.post("/sites/{domain}/push", status_code=status.HTTP_202_ACCEPTED)
def push(
    domain: str, body: PushBody, request: Request, principal: CurrentPrincipal, db: Db, agent: Agent
):
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    live = next(
        (s.get("staging_of") for s in call_agent(agent, "wordpress.list") if s["domain"] == domain),
        None,
    )
    if live:
        _authorize(db, principal, live)
    job = call_agent(agent, "wordpress.push", {"domain": domain, **body.model_dump()})
    _audit(db, request, principal, "wordpress.push", f"{domain} → {live}")
    return job


@router.post("/sites/{domain}/clone", status_code=status.HTTP_202_ACCEPTED)
def clone(
    domain: str,
    body: CloneBody,
    request: Request,
    principal: CurrentPrincipal,
    db: Db,
    agent: Agent,
):
    domain = _normalize(domain)
    target = _normalize(body.target)
    _authorize(db, principal, domain)
    created = False
    if target in _owners(db):
        _authorize(db, principal, target)
    elif principal.user.role == "admin":
        _create_site(db, agent, target, domain)
        created = True
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"create the website {target} first, then copy to it"
        )
    try:
        job = call_agent(agent, "wordpress.clone", {"domain": domain, "target": target})
    except HTTPException:
        if created:
            _remove_site(db, agent, target)
        raise
    _audit(db, request, principal, "wordpress.clone", f"{domain} → {target}")
    return job


def _may_see(job: dict[str, Any], principal: Principal, visible: set[str]) -> bool:
    if not job["kind"].startswith("wp-"):
        return False
    if principal.user.role == "admin":
        return True
    return job.get("domain") in visible and job.get("target", job.get("domain")) in visible


@router.get("/jobs")
def list_jobs(principal: CurrentPrincipal, db: Db, agent: Agent):
    visible = _visible_domains(db, principal)
    jobs = call_agent(agent, "backup.jobs", {"limit": 100})
    return {"jobs": [j for j in jobs if _may_see(j, principal, visible)][:20]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, principal: CurrentPrincipal, db: Db, agent: Agent):
    job = call_agent(agent, "backup.job.get", {"id": job_id})
    if not _may_see(job, principal, _visible_domains(db, principal)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
    return job
