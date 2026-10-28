"""Minimal Cloudflare API client for DNS (zones and records).

Uses an API token with the "Zone: Read" and "DNS: Edit" permissions.
Records are converted to and from Tela's own record format (see dnsrecords).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .registry import ActionError

API = "https://api.cloudflare.com/client/v4"
_ZONE_ID = re.compile(r"^[0-9a-f]{32}$")


class Cloudflare:
    def __init__(self, token: str, timeout: float = 20) -> None:
        self.token = token
        self.timeout = timeout

    # -- transport ---------------------------------------------------------
    def request(self, method: str, path: str, body: dict | None = None,
                query: dict | None = None) -> Any:  # fmt: skip
        url = API + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 (fixed https URL)
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "Tela hosting panel",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                raise ActionError(f"Cloudflare returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ActionError(f"cannot reach Cloudflare: {exc}") from exc
        if not payload.get("success", False):
            errors = payload.get("errors") or [{"message": "unknown error"}]
            message = "; ".join(str(e.get("message", e)) for e in errors)
            raise ActionError(f"Cloudflare: {message}")
        return payload

    def _paged(self, path: str, query: dict | None = None) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            payload = self.request("GET", path, query={**(query or {}), "page": page,
                                                       "per_page": 100})  # fmt: skip
            results.extend(payload.get("result") or [])
            info = payload.get("result_info") or {}
            if page >= int(info.get("total_pages", 1) or 1):
                return results
            page += 1

    # -- account -----------------------------------------------------------
    def verify(self) -> str:
        """Check the token; returns its status ("active")."""
        payload = self.request("GET", "/user/tokens/verify")
        return str((payload.get("result") or {}).get("status", "unknown"))

    def zones(self) -> list[dict[str, str]]:
        return [
            {"id": z["id"], "name": z["name"], "status": z.get("status", "")}
            for z in self._paged("/zones")
        ]

    # -- records -----------------------------------------------------------
    @staticmethod
    def _check_zone_id(zone_id: str) -> str:
        if not _ZONE_ID.match(zone_id or ""):
            raise ActionError("invalid Cloudflare zone id")
        return zone_id

    def records(self, zone_id: str, zone: str) -> list[dict[str, Any]]:
        rows = self._paged(f"/zones/{self._check_zone_id(zone_id)}/dns_records")
        return [r for r in (from_cloudflare(row, zone) for row in rows) if r is not None]

    def create(self, zone_id: str, zone: str, record: dict[str, Any]) -> dict[str, Any]:
        payload = self.request("POST", f"/zones/{self._check_zone_id(zone_id)}/dns_records",
                               body=to_cloudflare(record, zone))  # fmt: skip
        return from_cloudflare(payload["result"], zone) or record

    def update(self, zone_id: str, zone: str, record: dict[str, Any]) -> dict[str, Any]:
        rid = _record_id(record["id"])
        payload = self.request("PUT", f"/zones/{self._check_zone_id(zone_id)}/dns_records/{rid}",
                               body=to_cloudflare(record, zone))  # fmt: skip
        return from_cloudflare(payload["result"], zone) or record

    def delete(self, zone_id: str, record_id: str) -> None:
        rid = _record_id(record_id)
        self.request("DELETE", f"/zones/{self._check_zone_id(zone_id)}/dns_records/{rid}")


def _record_id(value: str) -> str:
    if not re.match(r"^[0-9a-f]{32}$", value or ""):
        raise ActionError("invalid record id")
    return value


def _relative(name: str, zone: str) -> str:
    name = name.lower().rstrip(".")
    if name == zone:
        return "@"
    return name[: -len(zone) - 1] if name.endswith("." + zone) else name


def from_cloudflare(row: dict[str, Any], zone: str) -> dict[str, Any] | None:
    rtype = row.get("type")
    if rtype not in ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA"):
        return None  # types Tela doesn't manage are left alone
    content = str(row.get("content", ""))
    priority = row.get("priority")
    data = row.get("data") or {}
    if rtype == "SRV" and data:
        priority = data.get("priority", priority)
        content = f"{data.get('weight', 0)} {data.get('port', 0)} {data.get('target', '')}"
    elif rtype == "CAA" and data:
        content = f'{data.get("flags", 0)} {data.get("tag", "issue")} "{data.get("value", "")}"'
    elif rtype == "TXT" and content.startswith('"') and content.endswith('"'):
        content = content[1:-1]
    return {
        "id": row["id"],
        "name": _relative(str(row.get("name", zone)), zone),
        "type": rtype,
        "content": content,
        "ttl": int(row.get("ttl", 1)),
        "priority": priority if rtype in ("MX", "SRV") else None,
        "proxied": bool(row.get("proxied", False)),
    }


def to_cloudflare(record: dict[str, Any], zone: str) -> dict[str, Any]:
    name = zone if record["name"] == "@" else f"{record['name']}.{zone}"
    body: dict[str, Any] = {"type": record["type"], "name": name, "ttl": record["ttl"]}
    if record["type"] == "SRV":
        weight, port, target = record["content"].split()
        body["data"] = {"priority": record["priority"], "weight": int(weight),
                        "port": int(port), "target": target}  # fmt: skip
    elif record["type"] == "CAA":
        flags, tag, value = record["content"].split(" ", 2)
        body["data"] = {"flags": int(flags), "tag": tag, "value": value.strip('"')}
    else:
        body["content"] = record["content"]
    if record["type"] == "MX":
        body["priority"] = record["priority"]
    if record["type"] in ("A", "AAAA", "CNAME"):
        body["proxied"] = bool(record.get("proxied"))
    return body
