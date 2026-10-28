"""DNS records: validation, normalization and BIND zone files.

A record looks the same whichever provider holds it:

    {"id": "...", "name": "www", "type": "A", "content": "203.0.113.5",
     "ttl": 3600, "priority": None, "proxied": False}

`name` is relative to the zone ("@" for the apex). `priority` is used by MX and
SRV. For SRV, `content` is "weight port target"; for CAA it is
'flags tag "value"'. `proxied` only means something on Cloudflare.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import time
from typing import Any

from .registry import ActionError

TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA")
PROXIABLE = ("A", "AAAA", "CNAME")
MIN_TTL, MAX_TTL = 60, 604800
DEFAULT_TTL = 3600

_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
_CAA = re.compile(r'^(\d{1,3})\s+(issue|issuewild|iodef)\s+"([^"\\]*)"$')


def new_id() -> str:
    return secrets.token_hex(8)


def _hostname(value: str, what: str) -> str:
    """A fully qualified host name, without the trailing dot."""
    name = value.strip().lower().rstrip(".")
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ActionError(f"invalid {what}: {value!r}") from exc
    labels = name.split(".")
    if not name or len(name) > 253 or not all(_LABEL.match(label) for label in labels):
        raise ActionError(f"invalid {what}: {value!r}")
    return name


def relative_name(value: object, zone: str) -> str:
    """Normalize a record name to be relative to the zone ('@' for the apex)."""
    if not isinstance(value, str):
        raise ActionError("record name must be a string")
    name = value.strip().lower().rstrip(".")
    if name in ("", "@", zone):
        return "@"
    if name.endswith("." + zone):
        name = name[: -len(zone) - 1]
    labels = name.split(".")
    for i, label in enumerate(labels):
        if label == "*" and i == 0:
            continue
        if not _LABEL.match(label):
            raise ActionError(f"invalid record name: {value!r}")
    if len(name) + len(zone) + 1 > 253:
        raise ActionError(f"record name is too long: {value!r}")
    return name


def fqdn(name: str, zone: str) -> str:
    return zone if name == "@" else f"{name}.{zone}"


def _ttl(value: object, allow_auto: bool) -> int:
    if value in (None, ""):
        return 1 if allow_auto else DEFAULT_TTL
    if not isinstance(value, int) or isinstance(value, bool):
        raise ActionError("TTL must be a number of seconds")
    if allow_auto and value == 1:
        return 1
    if not MIN_TTL <= value <= MAX_TTL:
        raise ActionError(f"TTL must be between {MIN_TTL} and {MAX_TTL} seconds")
    return value


def _priority(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 65535:
        raise ActionError("priority must be a number from 0 to 65535")
    return value


def validate(record: dict[str, Any], zone: str, *, cloudflare: bool = False) -> dict[str, Any]:
    """Return a clean copy of `record` or raise ActionError."""
    if not isinstance(record, dict):
        raise ActionError("record must be an object")
    rtype = str(record.get("type", "")).upper()
    if rtype not in TYPES:
        raise ActionError(f"record type must be one of {', '.join(TYPES)}")
    name = relative_name(record.get("name", "@"), zone)
    raw = record.get("content")
    if not isinstance(raw, str) or not raw.strip():
        raise ActionError("record content is required")
    content = raw.strip()
    priority = None

    if rtype == "A":
        try:
            content = str(ipaddress.IPv4Address(content))
        except ValueError as exc:
            raise ActionError(f"not an IPv4 address: {content!r}") from exc
    elif rtype == "AAAA":
        try:
            content = str(ipaddress.IPv6Address(content))
        except ValueError as exc:
            raise ActionError(f"not an IPv6 address: {content!r}") from exc
    elif rtype in ("CNAME", "NS"):
        content = _hostname(content, "target host name")
        if rtype == "CNAME" and name == "@" and not cloudflare:
            raise ActionError("a CNAME can't be used on the domain itself; use an A record")
    elif rtype == "MX":
        priority = _priority(record.get("priority", 10))
        content = _hostname(content, "mail server")
    elif rtype == "TXT":
        if len(content) > 4000:
            raise ActionError("TXT records can be at most 4000 characters")
        if any(ord(c) < 32 for c in content):
            raise ActionError("TXT records can't contain control characters")
    elif rtype == "SRV":
        priority = _priority(record.get("priority", 10))
        parts = content.split()
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ActionError(
                'SRV content must be "weight port target", e.g. "5 5060 sip.example.com"'
            )
        weight, port = int(parts[0]), int(parts[1])
        if weight > 65535 or not 0 < port <= 65535:
            raise ActionError("SRV weight or port is out of range")
        content = f"{weight} {port} {_hostname(parts[2], 'SRV target')}"
    elif rtype == "CAA":
        match = _CAA.match(content)
        if not match or int(match.group(1)) > 255:
            raise ActionError('CAA content must look like: 0 issue "letsencrypt.org"')
        content = f'{int(match.group(1))} {match.group(2)} "{match.group(3)}"'

    proxied = bool(record.get("proxied", False)) and cloudflare and rtype in PROXIABLE
    return {
        "id": str(record.get("id") or new_id()),
        "name": name,
        "type": rtype,
        "content": content,
        "ttl": _ttl(record.get("ttl"), allow_auto=cloudflare),
        "priority": priority,
        "proxied": proxied,
    }


# ---------------------------------------------------------------------------
# BIND zone files (read by PowerDNS's bind backend)
# ---------------------------------------------------------------------------


def next_serial(current: int, now: float | None = None) -> int:
    """Date-based serial (YYYYMMDDnn) that always increases."""
    today = int(time.strftime("%Y%m%d", time.gmtime(now))) * 100
    return max(today, current + 1)


def _txt(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    chunks = [escaped[i : i + 250] for i in range(0, len(escaped), 250)] or [""]
    return " ".join(f'"{chunk}"' for chunk in chunks)


def _target(host: str) -> str:
    return host + "."


def render_zone(zone: str, records: list[dict[str, Any]], serial: int, nameservers: list[str],
                hostmaster: str | None = None) -> str:  # fmt: skip
    primary = _target(nameservers[0]) if nameservers else f"ns1.{zone}."
    mbox = (hostmaster or f"hostmaster.{zone}").replace("@", ".") + "."
    lines = [
        "; Generated by Tela. Changes here are overwritten.",
        f"$ORIGIN {zone}.",
        f"$TTL {DEFAULT_TTL}",
        f"@ IN SOA {primary} {mbox} ( {serial} 3600 900 1209600 300 )",
    ]
    has_apex_ns = any(r["type"] == "NS" and r["name"] == "@" for r in records)
    if not has_apex_ns:
        lines += [f"@ {DEFAULT_TTL} IN NS {_target(ns)}" for ns in nameservers]
    for r in sorted(records, key=lambda r: (r["name"] != "@", r["name"], r["type"])):
        rtype, content = r["type"], r["content"]
        if rtype == "TXT":
            value = _txt(content)
        elif rtype in ("CNAME", "NS"):
            value = _target(content)
        elif rtype == "MX":
            value = f"{r['priority']} {_target(content)}"
        elif rtype == "SRV":
            weight, port, target = content.split()
            value = f"{r['priority']} {weight} {port} {_target(target)}"
        else:
            value = content
        lines.append(f"{r['name']} {r['ttl']} IN {rtype} {value}")
    return "\n".join(lines) + "\n"
