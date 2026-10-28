"""Databases. They belong to a website; whoever may manage the website may manage
its databases."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from .. import auth
from ..deps import Agent, CurrentPrincipal, Db, client_ip
from .common import call_agent
from .websites import _authorize, _normalize, _owners, _visible

router = APIRouter(prefix="/api/databases", tags=["databases"])


class CreateBody(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, max_length=128)


class PasswordBody(BaseModel):
    password: str | None = Field(default=None, max_length=128)


class SignonBody(BaseModel):
    name: str | None = Field(default=None, max_length=64)


@router.get("")
def list_databases(principal: CurrentPrincipal, db: Db, agent: Agent):
    owners = _owners(db)
    rows = call_agent(agent, "database.list")
    return {"databases": [r for r in rows if _visible(principal, owners.get(r["domain"]))]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_database(
    body: CreateBody, request: Request, principal: CurrentPrincipal, db: Db, agent: Agent
):
    domain = _normalize(body.domain)
    _authorize(db, principal, domain)
    result = call_agent(
        agent,
        "database.create",
        {"domain": domain, "name": body.name, "password": body.password or None},
    )
    auth.audit(db, "database.create", user_id=principal.user.id, ip=client_ip(request),
               detail=f"{domain}: {result['name']}")  # fmt: skip
    # The password is only ever shown here; Tela doesn't store it.
    return result


@router.post("/{domain}/{name}/password")
def change_password(
    domain: str, name: str, body: PasswordBody, request: Request, principal: CurrentPrincipal,
    db: Db, agent: Agent,
):  # fmt: skip
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    result = call_agent(
        agent, "database.password", {"domain": domain, "name": name, "password": body.password}
    )
    auth.audit(db, "database.password", user_id=principal.user.id, ip=client_ip(request),
               detail=f"{domain}: {result['name']}")  # fmt: skip
    return result


@router.delete("/{domain}/{name}")
def delete_database(
    domain: str, name: str, request: Request, principal: CurrentPrincipal, db: Db, agent: Agent
):
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    result = call_agent(agent, "database.delete", {"domain": domain, "name": name})
    auth.audit(db, "database.delete", user_id=principal.user.id, ip=client_ip(request),
               detail=f"{domain}: {result['name']}")  # fmt: skip
    return result


@router.post("/{domain}/phpmyadmin")
def phpmyadmin(
    domain: str, body: SignonBody, request: Request, principal: CurrentPrincipal, db: Db,
    agent: Agent,
):  # fmt: skip
    domain = _normalize(domain)
    _authorize(db, principal, domain)
    result = call_agent(agent, "phpmyadmin.signon", {"domain": domain, "name": body.name})
    auth.audit(db, "phpmyadmin.signon", user_id=principal.user.id, ip=client_ip(request),
               detail=domain)  # fmt: skip
    return result
