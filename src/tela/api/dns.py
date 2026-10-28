"""DNS. Administrators set up providers and zones; whoever owns a zone manages
its records."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import auth
from ..deps import AdminPrincipal, Agent, CurrentPrincipal, Db, Principal, client_ip
from .common import call_agent
from .websites import _normalize

router = APIRouter(prefix="/api/dns", tags=["dns"])


class SettingsBody(BaseModel):
    public_ipv4: str | None = Field(default=None, max_length=45)
    public_ipv6: str | None = Field(default=None, max_length=45)
    nameservers: list[str] | None = Field(default=None, max_length=6)
    cloudflare_token: str | None = Field(default=None, max_length=200)


class ZoneBody(BaseModel):
    zone: str = Field(min_length=1, max_length=253)
    provider: str = Field(pattern="^(local|cloudflare)$")
    cf_zone_id: str | None = Field(default=None, max_length=64)
    with_defaults: bool = True
    owner_id: int | None = None


class RecordBody(BaseModel):
    name: str = Field(default="@", max_length=253)
    type: str = Field(max_length=10)
    content: str = Field(max_length=4096)
    ttl: int | None = None
    priority: int | None = None
    proxied: bool = False


def _owners(db: sqlite3.Connection) -> dict[str, int | None]:
    return {r["zone"]: r["owner_id"] for r in db.execute("SELECT zone, owner_id FROM dns_zones")}


def _may_manage(principal: Principal, owner_id: int | None) -> bool:
    return principal.user.role == "admin" or owner_id == principal.user.id


def _authorize(db: sqlite3.Connection, principal: Principal, zone: str) -> None:
    row = db.execute("SELECT owner_id FROM dns_zones WHERE zone = ?", (zone,)).fetchone()
    if principal.user.role == "admin":
        return
    if row is None or row["owner_id"] != principal.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no DNS zone {zone}")


def _record_params(body: RecordBody) -> dict[str, Any]:
    data = body.model_dump()
    if data["priority"] is None:
        data.pop("priority")
    return data


@router.get("/status")
def dns_status(_principal: CurrentPrincipal, agent: Agent):
    return call_agent(agent, "dns.status")


@router.put("/settings")
def update_settings(body: SettingsBody, request: Request, admin: AdminPrincipal, db: Db,
                    agent: Agent):  # fmt: skip
    params = body.model_dump(exclude_unset=True)
    result = call_agent(agent, "dns.settings.update", params)
    changed = ", ".join(sorted(params))
    auth.audit(db, "dns.settings", user_id=admin.user.id, ip=client_ip(request), detail=changed)
    return result


@router.get("/cloudflare/zones")
def cloudflare_zones(_admin: AdminPrincipal, agent: Agent):
    return {"zones": call_agent(agent, "dns.cloudflare.zones")}


@router.get("/zones")
def list_zones(principal: CurrentPrincipal, db: Db, agent: Agent):
    owners = _owners(db)
    zones = call_agent(agent, "dns.zone.list")
    return {"zones": [z for z in zones if _may_manage(principal, owners.get(z["zone"]))]}


@router.post("/zones", status_code=status.HTTP_201_CREATED)
def create_zone(body: ZoneBody, request: Request, admin: AdminPrincipal, db: Db, agent: Agent):
    zone = _normalize(body.zone)
    owner_id = body.owner_id
    if owner_id is None:
        # Default to whoever owns the website with this domain.
        row = db.execute("SELECT owner_id FROM websites WHERE domain = ?", (zone,)).fetchone()
        owner_id = row["owner_id"] if row else admin.user.id
    if not db.execute("SELECT 1 FROM users WHERE id = ?", (owner_id,)).fetchone():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "owner does not exist")
    result = call_agent(
        agent,
        "dns.zone.create",
        {"zone": zone, "provider": body.provider, "cf_zone_id": body.cf_zone_id,
         "with_defaults": body.with_defaults},
    )  # fmt: skip
    db.execute(
        "INSERT INTO dns_zones (zone, owner_id, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(zone) DO UPDATE SET owner_id = excluded.owner_id",
        (zone, owner_id, int(time.time())),
    )
    auth.audit(db, "dns.zone.create", user_id=admin.user.id, ip=client_ip(request),
               detail=f"{zone} ({body.provider})")  # fmt: skip
    return result


@router.delete("/zones/{zone}")
def delete_zone(zone: str, request: Request, admin: AdminPrincipal, db: Db, agent: Agent):
    zone = _normalize(zone)
    result = call_agent(agent, "dns.zone.delete", {"zone": zone})
    db.execute("DELETE FROM dns_zones WHERE zone = ?", (zone,))
    auth.audit(db, "dns.zone.delete", user_id=admin.user.id, ip=client_ip(request), detail=zone)
    return result


@router.get("/zones/{zone}/records")
def list_records(zone: str, principal: CurrentPrincipal, db: Db, agent: Agent):
    zone = _normalize(zone)
    _authorize(db, principal, zone)
    return call_agent(agent, "dns.record.list", {"zone": zone})


@router.post("/zones/{zone}/records", status_code=status.HTTP_201_CREATED)
def create_record(zone: str, body: RecordBody, request: Request, principal: CurrentPrincipal,
                  db: Db, agent: Agent):  # fmt: skip
    zone = _normalize(zone)
    _authorize(db, principal, zone)
    record = call_agent(agent, "dns.record.create", {"zone": zone, "record": _record_params(body)})
    auth.audit(db, "dns.record.create", user_id=principal.user.id, ip=client_ip(request),
               detail=f"{zone}: {record['type']} {record['name']}")  # fmt: skip
    return record


@router.put("/zones/{zone}/records/{record_id}")
def update_record(zone: str, record_id: str, body: RecordBody, request: Request,
                  principal: CurrentPrincipal, db: Db, agent: Agent):  # fmt: skip
    zone = _normalize(zone)
    _authorize(db, principal, zone)
    params = {**_record_params(body), "id": record_id}
    record = call_agent(agent, "dns.record.update", {"zone": zone, "record": params})
    auth.audit(db, "dns.record.update", user_id=principal.user.id, ip=client_ip(request),
               detail=f"{zone}: {record['type']} {record['name']}")  # fmt: skip
    return record


@router.delete("/zones/{zone}/records/{record_id}")
def delete_record(zone: str, record_id: str, request: Request, principal: CurrentPrincipal,
                  db: Db, agent: Agent):  # fmt: skip
    zone = _normalize(zone)
    _authorize(db, principal, zone)
    result = call_agent(agent, "dns.record.delete", {"zone": zone, "id": record_id})
    auth.audit(db, "dns.record.delete", user_id=principal.user.id, ip=client_ip(request),
               detail=f"{zone}: {record_id}")  # fmt: skip
    return result
