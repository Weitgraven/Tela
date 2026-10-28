"""Input validation. The agent never trusts its caller: every value that ends up
in a path, a config file or a command line is checked here first."""

from __future__ import annotations

import re

from .registry import ActionError

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_PHP = re.compile(r"^8\.[0-9]$")
_EMAIL = re.compile(r"^[^@\s<>\"']+@[a-z0-9.-]+\.[a-z]{2,}$")


def domain(value: object) -> str:
    """Return a normalized, IDNA-encoded domain name or raise ActionError."""
    if not isinstance(value, str):
        raise ActionError("domain must be a string")
    name = value.strip().lower().rstrip(".")
    if name.startswith("www.") and name.count(".") >= 2:
        # Sites are keyed by their bare domain; www is added as an alias.
        name = name[4:]
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ActionError(f"invalid domain name: {value!r}") from exc
    labels = name.split(".")
    if (
        len(name) > 253
        or len(labels) < 2
        or not all(_LABEL.match(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise ActionError(f"invalid domain name: {value!r}")
    return name


def alias(value: object) -> str:
    """Like domain(), but keeps a leading www. and allows a leading wildcard."""
    if not isinstance(value, str):
        raise ActionError("alias must be a string")
    name = value.strip().lower().rstrip(".")
    wildcard = name.startswith("*.")
    rest = name[2:] if wildcard else name
    if rest.startswith("www."):
        normalized = "www." + domain(rest[4:]) if rest.count(".") >= 2 else domain(rest)
    else:
        normalized = domain(rest)
    return ("*." if wildcard else "") + normalized


def aliases(value: object, primary: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 50:
        raise ActionError("aliases must be a list of at most 50 names")
    result: list[str] = []
    for item in value:
        name = alias(item)
        if name != primary and name not in result:
            result.append(name)
    return result


def php_version(value: object) -> str:
    if not isinstance(value, str) or not _PHP.match(value):
        raise ActionError(f"invalid PHP version: {value!r}")
    return value


def email(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 254 or not _EMAIL.match(value.lower()):
        raise ActionError(f"invalid email address: {value!r}")
    return value.lower()


def boolean(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ActionError("expected true or false")
    return value
