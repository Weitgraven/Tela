"""Email. Administrators run the mail server (host name, certificate); whoever
owns a mail domain manages its mailboxes and aliases. A user may add email for
domains of their own websites."""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import auth
from ..deps import AdminPrincipal, Agent, CurrentPrincipal, Db, Principal, client_ip
from .common import call_agent
from .websites import _normalize

router = APIRouter(prefix="/api/mail", tags=["mail"])


class SettingsBody(BaseModel):
    hostname: str = Field(min_length=1, max_length=253)


class SslBody(BaseModel):
    email: str | None = Field(default=None, max_length=254)


class DomainBody(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    dns: bool = True
    owner_id: int | None = None


class DeleteBody(BaseModel):
    remove_mail: bool = True


class MailboxBody(BaseModel):
    local: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, max_length=128)
    quota_mb: int = Field(default=0, ge=0, le=1024 * 1024)
    name: str | None = Field(default=None, max_length=100)


class MailboxUpdate(BaseModel):
    password: str | None = Field(default=None, max_length=128)
    quota_mb: int | None = Field(default=None, ge=0, le=1024 * 1024)
    enabled: bool | None = None
    name: str | None = Field(default=None, max_length=100)


class AliasBody(BaseModel):
    local: str = Field(min_length=1, max_length=64)
    destinations: list[str] = Field(min_length=1, max_length=50)


def _owners(db: sqlite3.Connection) -> dict[str, int | None]:
    return {
        r["domain"]: r["owner_id"] for r in db.execute("SELECT domain, owner_id FROM mail_domains")
    }


def _authorize(db: sqlite3.Connection, principal: Principal, domain: str) -> None:
    if principal.user.role == "admin":
        return
    row = db.execute("SELECT owner_id FROM mail_domains WHERE domain = ?", (domain,)).fetchone()
    if row is None or row["owner_id"] != principal.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{domain} isn't set up for email")


def _audit(db: sqlite3.Connection, request: Request, principal: Principal, event: str,
           detail: str) -> None:  # fmt: skip
    auth.audit(db, event, user_id=principal.user.id, ip=client_ip(request), detail=detail)


@router.get("/status")
def mail_status(_principal: CurrentPrincipal, agent: Agent):
    return call_agent(agent, "mail.status")


@router.put("/settings")
def update_settings(body: SettingsBody, request: Request, admin: AdminPrincipal, db: Db,
                    agent: Agent):  # fmt: skip
    hostname = _normalize(body.hostname)
    taken = db.execute("SELECT 1 FROM websites WHERE domain = ?", (hostname,)).fetchone()
    if taken:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{hostname} is already a website")
    result = call_agent(agent, "mail.settings.update", {"hostname": hostname}, timeout=120)
    _audit(db, request, admin, "mail.settings", hostname)
    return result


@router.post("/ssl")
def issue_ssl(body: SslBody, request: Request, admin: AdminPrincipal, db: Db, agent: Agent):
    result = call_agent(agent, "mail.hostname_ssl", {"email": body.email}, timeout=330)
    _audit(db, request, admin, "mail.ssl", result.get("hostname", ""))
    return result


@router.get("/domains")
def list_domains(principal: CurrentPrincipal, db: Db, agent: Agent):
    owners = _owners(db)
    domains = call_agent(agent, "mail.domain.list")
    admin = principal.user.role == "admin"
    return {"domains": [d for d in domains
                        if admin or owners.get(d["domain"]) == principal.user.id]}  # fmt: skip


@router.post("/domains", status_code=status.HTTP_201_CREATED)
def create_domain(body: DomainBody, request: Request, principal: CurrentPrincipal, db: Db,
                  agent: Agent):  # fmt: skip
    domain = _normalize(body.domain)
    site = db.execute("SELECT owner_id FROM websites WHERE domain = ?", (domain,)).fetchone()
    if principal.user.role == "admin":
        owner_id = body.owner_id
        if owner_id is None:
            owner_id = site["owner_id"] if site else principal.user.id
        if not db.execute("SELECT 1 FROM users WHERE id = ?", (owner_id,)).fetchone():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "owner does not exist")
    else:
        # Users may add email only for domains of their own websites.
        if site is None or site["owner_id"] != principal.user.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "you can add email only for your own websites' domains"
            )
        owner_id = principal.user.id
    # Records go into the zone only if this user may manage that zone.
    add_dns = body.dns
    if add_dns and principal.user.role != "admin":
        zone = db.execute("SELECT owner_id FROM dns_zones WHERE zone = ?", (domain,)).fetchone()
        add_dns = zone is not None and zone["owner_id"] == principal.user.id
    result = call_agent(agent, "mail.domain.create", {"domain": domain, "dns": add_dns})
    db.execute(
        "INSERT INTO mail_domains (domain, owner_id, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(domain) DO UPDATE SET owner_id = excluded.owner_id",
        (domain, owner_id, int(time.time())),
    )
    _audit(db, request, principal, "mail.domain.create", domain)
    return result


@router.get("/domains/{domain}")
def get_domain(domain: str, principal: CurrentPrincipal, db: Db, agent: Agent):
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    return call_agent(agent, "mail.domain.get", {"domain": domain})


@router.delete("/domains/{domain}")
def delete_domain(domain: str, request: Request, principal: CurrentPrincipal,
                  db: Db, agent: Agent, body: DeleteBody | None = None):  # fmt: skip
    body = body or DeleteBody()
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    result = call_agent(agent, "mail.domain.delete",
                        {"domain": domain, "remove_mail": body.remove_mail})  # fmt: skip
    db.execute("DELETE FROM mail_domains WHERE domain = ?", (domain,))
    _audit(db, request, principal, "mail.domain.delete", domain)
    return result


@router.post("/domains/{domain}/dns")
def add_dns_records(domain: str, request: Request, principal: CurrentPrincipal, db: Db,
                    agent: Agent):  # fmt: skip
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    info = call_agent(agent, "mail.domain.get", {"domain": domain})
    zone = info.get("dns_zone")
    if principal.user.role != "admin" and zone:
        row = db.execute("SELECT owner_id FROM dns_zones WHERE zone = ?", (zone,)).fetchone()
        if row is None or row["owner_id"] != principal.user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"you can't change the DNS zone {zone}")
    result = call_agent(agent, "mail.domain.dns", {"domain": domain})
    _audit(db, request, principal, "mail.domain.dns", domain)
    return result


@router.post("/domains/{domain}/mailboxes", status_code=status.HTTP_201_CREATED)
def create_mailbox(domain: str, body: MailboxBody, request: Request, principal: CurrentPrincipal,
                   db: Db, agent: Agent):  # fmt: skip
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    result = call_agent(agent, "mail.mailbox.create", {"domain": domain, **body.model_dump()})
    _audit(db, request, principal, "mail.mailbox.create", result["address"])
    return result


@router.put("/domains/{domain}/mailboxes/{local}")
def update_mailbox(domain: str, local: str, body: MailboxUpdate, request: Request,
                   principal: CurrentPrincipal, db: Db, agent: Agent):  # fmt: skip
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    params = body.model_dump(exclude_unset=True)
    result = call_agent(agent, "mail.mailbox.update", {"domain": domain, "local": local, **params})
    _audit(db, request, principal, "mail.mailbox.update",
           f"{result['address']}: {', '.join(sorted(params)) or 'nothing'}")  # fmt: skip
    return result


@router.delete("/domains/{domain}/mailboxes/{local}")
def delete_mailbox(domain: str, local: str, request: Request,
                   principal: CurrentPrincipal, db: Db, agent: Agent,
                   body: DeleteBody | None = None):  # fmt: skip
    body = body or DeleteBody()
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    params = {"domain": domain, "local": local, "remove_mail": body.remove_mail}
    result = call_agent(agent, "mail.mailbox.delete", params)
    _audit(db, request, principal, "mail.mailbox.delete", result["address"])
    return result


@router.post("/domains/{domain}/aliases", status_code=status.HTTP_201_CREATED)
def create_alias(domain: str, body: AliasBody, request: Request, principal: CurrentPrincipal,
                 db: Db, agent: Agent):  # fmt: skip
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    result = call_agent(agent, "mail.alias.create", {"domain": domain, **body.model_dump()})
    _audit(db, request, principal, "mail.alias.create", f"{result['local']}@{domain}")
    return result


@router.delete("/domains/{domain}/aliases/{local}")
def delete_alias(domain: str, local: str, request: Request, principal: CurrentPrincipal, db: Db,
                 agent: Agent):  # fmt: skip
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    result = call_agent(agent, "mail.alias.delete", {"domain": domain, "local": local})
    _audit(db, request, principal, "mail.alias.delete", f"{local}@{domain}")
    return result
