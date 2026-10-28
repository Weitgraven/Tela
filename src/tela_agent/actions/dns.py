"""DNS zones and records, held either by Tela's own PowerDNS ("local") or by
Cloudflare ("cloudflare").

State lives in <dns_dir>/settings.json (server IPs, nameservers, Cloudflare
token) and <dns_dir>/zones/<zone>.json. For local zones the JSON file holds the
records; for Cloudflare zones Cloudflare is the source of truth and the file
only links the domain to its Cloudflare zone id.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import threading
from pathlib import Path
from typing import Any

from .. import dnsrecords, settings, sysops, validate
from ..cloudflare import Cloudflare
from ..powerdns import PowerDNS
from ..registry import ActionError, action

log = logging.getLogger("tela-agent.dns")
_lock = threading.RLock()
PROVIDERS = ("local", "cloudflare")


def cloudflare_client(token: str) -> Cloudflare:
    """Replaced by a fake in tests."""
    return Cloudflare(token)


def powerdns(paths: settings.Paths) -> PowerDNS:
    """Replaced by a fake in tests."""
    return PowerDNS(paths.pdns_dir)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _settings_file(paths: settings.Paths) -> Path:
    return paths.dns_dir / "settings.json"


def _zone_file(paths: settings.Paths, zone: str) -> Path:
    return paths.dns_dir / "zones" / f"{zone}.json"


def load_settings(paths: settings.Paths) -> dict[str, Any]:
    defaults = {"public_ipv4": "", "public_ipv6": "", "nameservers": [], "cloudflare_token": ""}
    try:
        data = json.loads(_settings_file(paths).read_text())
    except (OSError, ValueError):
        return defaults
    return {**defaults, **{k: v for k, v in data.items() if k in defaults}}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sysops.write_atomic(path, json.dumps(data, indent=2) + "\n", 0o600)


def load_zones(paths: settings.Paths) -> list[dict[str, Any]]:
    folder = paths.dns_dir / "zones"
    if not folder.exists():
        return []
    return [json.loads(f.read_text()) for f in sorted(folder.glob("*.json"))]


def _load_zone(paths: settings.Paths, zone: str) -> dict[str, Any]:
    try:
        return json.loads(_zone_file(paths, zone).read_text())
    except OSError as exc:
        raise ActionError(f"no DNS zone {zone}") from exc


def _public(conf: dict[str, Any]) -> dict[str, Any]:
    token = conf.get("cloudflare_token") or ""
    return {
        "public_ipv4": conf["public_ipv4"],
        "public_ipv6": conf["public_ipv6"],
        "nameservers": conf["nameservers"],
        "cloudflare_connected": bool(token),
        "cloudflare_token_hint": f"…{token[-4:]}" if token else "",
    }


def _cf(conf: dict[str, Any]) -> Cloudflare:
    if not conf.get("cloudflare_token"):
        raise ActionError("connect Cloudflare first: add an API token in DNS settings")
    return cloudflare_client(conf["cloudflare_token"])


# ---------------------------------------------------------------------------
# Local zones: write every zone file and reload PowerDNS, restoring on failure
# ---------------------------------------------------------------------------


def _render_local(paths: settings.Paths, conf: dict[str, Any]) -> dict[str, str]:
    return {
        z["zone"]: dnsrecords.render_zone(z["zone"], z["records"], z["serial"], conf["nameservers"])
        for z in load_zones(paths)
        if z["provider"] == "local"
    }


def _apply_local(paths: settings.Paths, zone: dict[str, Any] | None, *, removed: str | None = None,
                 previous: dict[str, Any] | None = None) -> None:  # fmt: skip
    """Save `zone` (or remove `removed`), write PowerDNS's files and reload.

    `previous` is the zone's saved state before the change, restored on failure.
    """
    pdns = powerdns(paths)
    if not pdns.available:
        raise ActionError(
            "Tela's DNS server isn't installed. Run the installer with --with-dns-server, "
            "or use Cloudflare."
        )
    conf = load_settings(paths)
    name = zone["zone"] if zone else removed
    assert name is not None
    path = _zone_file(paths, name)
    if zone:
        zone["serial"] = dnsrecords.next_serial(int(zone.get("serial", 0)))
        _save_json(path, zone)
    else:
        path.unlink(missing_ok=True)
    try:
        pdns.write(_render_local(paths, conf))
        pdns.reload(zone["zone"] if zone else None)
    except Exception:
        if previous:
            _save_json(path, previous)
        else:
            path.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            pdns.write(_render_local(paths, conf))
            pdns.reload()
        raise


# ---------------------------------------------------------------------------
# Records, whichever provider holds them
# ---------------------------------------------------------------------------


def _records(paths: settings.Paths, zone: dict[str, Any]) -> list[dict[str, Any]]:
    if zone["provider"] == "local":
        return list(zone["records"])
    return _cf(load_settings(paths)).records(zone["cf_zone_id"], zone["zone"])


def _add(paths: settings.Paths, zone: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    if zone["provider"] == "local":
        previous = json.loads(json.dumps(zone))
        _check_conflicts(zone["records"], record)
        zone["records"].append(record)
        _apply_local(paths, zone, previous=previous)
        return record
    return _cf(load_settings(paths)).create(zone["cf_zone_id"], zone["zone"], record)


def _check_conflicts(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    others = [r for r in records if r["name"] == record["name"] and r["id"] != record["id"]]
    if record["type"] == "CNAME" and others:
        raise ActionError(f"{record['name']} already has other records; a CNAME must be alone")
    if any(r["type"] == "CNAME" for r in others):
        raise ActionError(f"{record['name']} has a CNAME record; remove it first")
    for r in others:
        if (r["type"], r["content"], r.get("priority")) == (
            record["type"], record["content"], record.get("priority"),
        ):  # fmt: skip
            raise ActionError("this record already exists")


# ---------------------------------------------------------------------------
# Automatic records for websites
# ---------------------------------------------------------------------------


def zone_for(paths: settings.Paths, domain: str) -> dict[str, Any] | None:
    """The zone that contains `domain` (the longest matching one)."""
    best = None
    for z in load_zones(paths):
        if (domain == z["zone"] or domain.endswith("." + z["zone"])) and (
            best is None or len(z["zone"]) > len(best["zone"])
        ):
            best = z
    return best


def ensure_site_records(paths: settings.Paths, domain: str, aliases: list[str]) -> str:
    """Point the website's names at this server, where the zone has no record yet.

    Returns a short summary for the user. Never raises: DNS is a convenience
    here, the website works without it.
    """
    try:
        conf = load_settings(paths)
        zone = zone_for(paths, domain)
        if zone is None:
            return "no DNS zone for this domain"
        if not conf["public_ipv4"] and not conf["public_ipv6"]:
            return "set the server's public IP in DNS settings to add records automatically"
        with _lock:
            existing = _records(paths, zone)
            taken = {(r["name"], r["type"]) for r in existing}
            names = {r["name"] for r in existing if r["type"] == "CNAME"}
            added = []
            for host in [domain, *aliases]:
                if host.startswith("*.") or not (host == zone["zone"] or
                                                 host.endswith("." + zone["zone"])):  # fmt: skip
                    continue
                name = dnsrecords.relative_name(host, zone["zone"])
                if name in names:
                    continue
                for rtype, ip in (("A", conf["public_ipv4"]), ("AAAA", conf["public_ipv6"])):
                    if ip and (name, rtype) not in taken:
                        record = dnsrecords.validate(
                            {"name": name, "type": rtype, "content": ip},
                            zone["zone"], cloudflare=zone["provider"] == "cloudflare",
                        )  # fmt: skip
                        _add(paths, zone, record)
                        added.append(f"{rtype} {dnsrecords.fqdn(name, zone['zone'])}")
                        zone = _load_zone(paths, zone["zone"])
        return ("added " + ", ".join(added)) if added else "records already present"
    except Exception as exc:
        log.warning("automatic DNS records for %s failed: %s", domain, exc)
        return f"could not add DNS records: {exc}"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@action("dns.status")
def status(_params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    return {
        "local_available": powerdns(paths).available,
        "settings": _public(load_settings(paths)),
    }


@action("dns.settings.update")
def update_settings(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    with _lock:
        conf = load_settings(paths)
        if "public_ipv4" in params:
            value = str(params["public_ipv4"] or "").strip()
            if value:
                try:
                    value = str(ipaddress.IPv4Address(value))
                except ValueError as exc:
                    raise ActionError(f"not an IPv4 address: {value!r}") from exc
            conf["public_ipv4"] = value
        if "public_ipv6" in params:
            value = str(params["public_ipv6"] or "").strip()
            if value:
                try:
                    value = str(ipaddress.IPv6Address(value))
                except ValueError as exc:
                    raise ActionError(f"not an IPv6 address: {value!r}") from exc
            conf["public_ipv6"] = value
        if "nameservers" in params:
            values = params["nameservers"] or []
            if not isinstance(values, list) or len(values) > 6:
                raise ActionError("nameservers must be a list of at most 6 host names")
            conf["nameservers"] = [validate.domain(v) for v in values if str(v).strip()]
        if "cloudflare_token" in params:
            token = str(params["cloudflare_token"] or "").strip()
            if token:
                if len(token) > 200 or not token.replace("-", "").replace("_", "").isalnum():
                    raise ActionError("that doesn't look like a Cloudflare API token")
                if cloudflare_client(token).verify() != "active":
                    raise ActionError("Cloudflare says this token isn't active")
            elif any(z["provider"] == "cloudflare" for z in load_zones(paths)):
                raise ActionError("remove the Cloudflare zones from Tela before disconnecting")
            conf["cloudflare_token"] = token
        _save_json(_settings_file(paths), conf)
        local_zones = [z for z in load_zones(paths) if z["provider"] == "local"]
    # Nameservers are part of every local zone's SOA and NS records: bump each
    # zone's serial (so secondaries notice) and reload it.
    if "nameservers" in params and local_zones and powerdns(paths).available:
        with _lock:
            for zone in local_zones:
                zone["serial"] = dnsrecords.next_serial(int(zone.get("serial", 0)))
                _save_json(_zone_file(paths, zone["zone"]), zone)
            pdns = powerdns(paths)
            pdns.write(_render_local(paths, conf))
            for zone in local_zones:
                pdns.reload(zone["zone"])
    return _public(conf)


@action("dns.cloudflare.zones")
def cloudflare_zones(_params: dict[str, Any]) -> list[dict[str, Any]]:
    paths = settings.load()
    linked = {z.get("cf_zone_id") for z in load_zones(paths)}
    return [{**z, "linked": z["id"] in linked} for z in _cf(load_settings(paths)).zones()]


@action("dns.zone.list")
def list_zones(_params: dict[str, Any]) -> list[dict[str, Any]]:
    paths = settings.load()
    return [
        {
            "zone": z["zone"],
            "provider": z["provider"],
            "records": len(z["records"]) if z["provider"] == "local" else None,
        }
        for z in load_zones(paths)
    ]


@action("dns.zone.create")
def create_zone(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    name = validate.domain(params.get("zone"))
    provider = params.get("provider")
    if provider not in PROVIDERS:
        raise ActionError("provider must be 'local' or 'cloudflare'")
    with_defaults = validate.boolean(params.get("with_defaults"), default=True)
    with _lock:
        if _zone_file(paths, name).exists():
            raise ActionError(f"the DNS zone {name} already exists")
        conf = load_settings(paths)
        if provider == "cloudflare":
            cf_zone_id = str(params.get("cf_zone_id") or "")
            match = next((z for z in _cf(conf).zones() if z["id"] == cf_zone_id), None)
            if match is None or match["name"] != name:
                raise ActionError(f"{name} isn't a zone in your Cloudflare account")
            zone = {"zone": name, "provider": "cloudflare", "cf_zone_id": cf_zone_id}
            _save_json(_zone_file(paths, name), zone)
        else:
            if len(conf["nameservers"]) < 1:
                raise ActionError("set the nameservers in DNS settings first")
            records = []
            if with_defaults:
                for rname in ("@", "www"):
                    for rtype, ip in (("A", conf["public_ipv4"]), ("AAAA", conf["public_ipv6"])):
                        if ip:
                            records.append(dnsrecords.validate(
                                {"name": rname, "type": rtype, "content": ip}, name))  # fmt: skip
            zone = {"zone": name, "provider": "local", "serial": 0, "records": records}
            _apply_local(paths, zone)
    if provider == "cloudflare" and with_defaults:
        ensure_site_records(paths, name, [f"www.{name}"])
    return {"zone": name, "provider": provider}


@action("dns.zone.delete")
def delete_zone(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    name = validate.domain(params.get("zone"))
    with _lock:
        zone = _load_zone(paths, name)
        if zone["provider"] == "local":
            _apply_local(paths, None, removed=name, previous=zone)
        else:
            # Only unlink: the zone and its records stay in Cloudflare.
            _zone_file(paths, name).unlink(missing_ok=True)
    return {"zone": name, "provider": zone["provider"]}


@action("dns.record.list")
def list_records(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    zone = _load_zone(paths, validate.domain(params.get("zone")))
    records = sorted(_records(paths, zone),
                     key=lambda r: (r["name"] != "@", r["name"], r["type"]))  # fmt: skip
    return {"zone": zone["zone"], "provider": zone["provider"], "records": records}


@action("dns.record.create")
def create_record(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    with _lock:
        zone = _load_zone(paths, validate.domain(params.get("zone")))
        record = dnsrecords.validate({**(params.get("record") or {}), "id": None}, zone["zone"],
                                     cloudflare=zone["provider"] == "cloudflare")  # fmt: skip
        return _add(paths, zone, record)


@action("dns.record.update")
def update_record(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    raw = params.get("record") or {}
    record_id = str(raw.get("id") or "")
    with _lock:
        zone = _load_zone(paths, validate.domain(params.get("zone")))
        record = dnsrecords.validate(raw, zone["zone"], cloudflare=zone["provider"] == "cloudflare")
        if zone["provider"] == "cloudflare":
            return _cf(load_settings(paths)).update(zone["cf_zone_id"], zone["zone"], record)
        previous = json.loads(json.dumps(zone))
        index = next((i for i, r in enumerate(zone["records"]) if r["id"] == record_id), None)
        if index is None:
            raise ActionError("no such record")
        _check_conflicts(zone["records"], record)
        zone["records"][index] = record
        _apply_local(paths, zone, previous=previous)
        return record


@action("dns.record.delete")
def delete_record(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    record_id = str(params.get("id") or "")
    with _lock:
        zone = _load_zone(paths, validate.domain(params.get("zone")))
        if zone["provider"] == "cloudflare":
            _cf(load_settings(paths)).delete(zone["cf_zone_id"], record_id)
            return {"id": record_id}
        previous = json.loads(json.dumps(zone))
        before = len(zone["records"])
        zone["records"] = [r for r in zone["records"] if r["id"] != record_id]
        if len(zone["records"]) == before:
            raise ActionError("no such record")
        _apply_local(paths, zone, previous=previous)
    return {"id": record_id}
