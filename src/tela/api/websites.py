"""Websites. The agent holds each site's configuration; the panel database records
who owns which site."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from tela_agent import validate
from tela_agent.registry import ActionError

from .. import auth
from ..deps import AdminPrincipal, Agent, CurrentPrincipal, Db, Principal, client_ip
from .common import call_agent

router = APIRouter(prefix="/api", tags=["websites"])

SSL_TIMEOUT = 330  # certbot may need a few minutes


class CreateBody(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    php_version: str = Field(min_length=1, max_length=8)
    include_www: bool = True
    aliases: list[str] = Field(default_factory=list, max_length=50)
    owner_id: int | None = None


class UpdateBody(BaseModel):
    php_version: str | None = Field(default=None, max_length=8)
    aliases: list[str] | None = Field(default=None, max_length=50)
    force_https: bool | None = None


class SslBody(BaseModel):
    email: str | None = Field(default=None, max_length=254)


class DeleteBody(BaseModel):
    confirm: str = Field(max_length=253)
    remove_files: bool = True


def _normalize(domain: str) -> str:
    try:
        return validate.domain(domain)
    except ActionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _owners(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = db.execute(
        "SELECT w.id, w.domain, w.owner_id, u.username FROM websites w"
        " LEFT JOIN users u ON u.id = w.owner_id"
    ).fetchall()
    return {
        r["domain"]: {
            "id": r["id"],
            "owner": {"id": r["owner_id"], "username": r["username"]} if r["owner_id"] else None,
        }
        for r in rows
    }


def _merge(site: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
    site = dict(site)
    site.pop("ssl_key", None)
    site["owner"] = meta["owner"] if meta else None
    return site


def _visible(principal: Principal, meta: dict[str, Any] | None) -> bool:
    if principal.user.role == "admin":
        return True
    return bool(meta and meta["owner"] and meta["owner"]["id"] == principal.user.id)


def _authorize(db: sqlite3.Connection, principal: Principal, domain: str) -> dict[str, Any] | None:
    meta = _owners(db).get(domain)
    if not _visible(principal, meta):
        # Same answer as a site that doesn't exist, so names aren't disclosed.
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no website {domain}")
    return meta


@router.get("/php/versions")
def php_versions(_principal: CurrentPrincipal, agent: Agent):
    return {"versions": call_agent(agent, "php.versions")}


@router.get("/websites")
def list_websites(principal: CurrentPrincipal, db: Db, agent: Agent):
    owners = _owners(db)
    sites = call_agent(agent, "website.list")
    return {
        "websites": [
            _merge(s, owners.get(s["domain"]))
            for s in sites
            if _visible(principal, owners.get(s["domain"]))
        ]
    }


@router.post("/websites", status_code=status.HTTP_201_CREATED)
def create_website(body: CreateBody, request: Request, admin: AdminPrincipal, db: Db, agent: Agent):
    owner_id = body.owner_id or admin.user.id
    if not db.execute("SELECT 1 FROM users WHERE id = ?", (owner_id,)).fetchone():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "owner does not exist")

    site = call_agent(
        agent,
        "website.create",
        {
            "domain": body.domain,
            "php_version": body.php_version,
            "include_www": body.include_www,
            "aliases": body.aliases,
        },
    )
    try:
        db.execute(
            "INSERT INTO websites (domain, owner_id, created_at) VALUES (?, ?, ?)",
            (site["domain"], owner_id, int(time.time())),
        )
    except sqlite3.IntegrityError:
        # A leftover row from a site removed outside the panel; take it over.
        db.execute("UPDATE websites SET owner_id = ? WHERE domain = ?", (owner_id, site["domain"]))
    auth.audit(db, "website.create", user_id=admin.user.id, ip=client_ip(request),
               detail=site["domain"])  # fmt: skip
    return _merge(site, _owners(db).get(site["domain"]))


@router.get("/websites/{domain}")
def get_website(domain: str, principal: CurrentPrincipal, db: Db, agent: Agent):
    domain = _normalize(domain)
    meta = _authorize(db, principal, domain)
    return _merge(call_agent(agent, "website.get", {"domain": domain}), meta)


@router.patch("/websites/{domain}")
def update_website(
    domain: str, body: UpdateBody, request: Request, principal: CurrentPrincipal, db: Db,
    agent: Agent,
):  # fmt: skip
    domain = _normalize(domain)
    meta = _authorize(db, principal, domain)
    params: dict[str, Any] = {"domain": domain}
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            params[key] = value
    site = call_agent(agent, "website.update", params)
    changed = ", ".join(k for k in params if k != "domain")
    auth.audit(db, "website.update", user_id=principal.user.id, ip=client_ip(request),
               detail=f"{domain}: {changed}")  # fmt: skip
    return _merge(site, meta)


@router.post("/websites/{domain}/ssl")
def issue_ssl(
    domain: str, body: SslBody, request: Request, principal: CurrentPrincipal, db: Db,
    agent: Agent,
):  # fmt: skip
    domain = _normalize(domain)
    meta = _authorize(db, principal, domain)
    site = call_agent(
        agent, "website.ssl_issue", {"domain": domain, "email": body.email}, timeout=SSL_TIMEOUT
    )
    auth.audit(db, "website.ssl_issue", user_id=principal.user.id, ip=client_ip(request),
               detail=domain)  # fmt: skip
    return _merge(site, meta)


@router.delete("/websites/{domain}")
def delete_website(
    domain: str, body: DeleteBody, request: Request, admin: AdminPrincipal, db: Db, agent: Agent
):
    domain = _normalize(domain)
    _authorize(db, admin, domain)
    if body.confirm.strip().lower() != domain:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "type the domain name to confirm")
    result = call_agent(
        agent, "website.delete", {"domain": domain, "remove_files": body.remove_files}
    )
    db.execute("DELETE FROM websites WHERE domain = ?", (domain,))
    auth.audit(db, "website.delete", user_id=admin.user.id, ip=client_ip(request),
               detail=f"{domain} files_removed={body.remove_files}")  # fmt: skip
    return result
