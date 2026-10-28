"""Databases: MariaDB databases that belong to a website, and phpMyAdmin sign-on.

Each database gets a MariaDB user with the same name and full rights on that one
database. Names are prefixed with the website's system user (``shop_wp``), so
databases of different websites can never collide.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import string
import time
from typing import Any

from .. import settings, sysops, validate
from ..mariadb import NAME_RE, MariaDB
from ..ols import Site
from ..registry import ActionError, action
from . import websites

MAX_NAME = 64
PASSWORD_ALPHABET = string.ascii_letters + string.digits
SSO_TOKEN_SECONDS = 60
SSO_USER_HOURS = 12


def server(paths: settings.Paths) -> MariaDB:
    """The MariaDB server to manage. Replaced by a fake in tests."""
    return MariaDB(paths.mysql_socket)


def generate_password(length: int = 24) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _password(value: object) -> str:
    if value in (None, ""):
        return generate_password()
    if not isinstance(value, str) or not 12 <= len(value) <= 128:
        raise ActionError("password must be 12 to 128 characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ActionError("password contains invalid characters")
    return value


def _prefix(site: Site) -> str:
    return site.user[:16] + "_"


def full_name(site: Site, value: object) -> str:
    """Turn the short name a user typed ("wp") into the real one ("shop_wp")."""
    if not isinstance(value, str):
        raise ActionError("database name must be a string")
    short = value.strip().lower()
    prefix = _prefix(site)
    if short.startswith(prefix):
        short = short[len(prefix) :]
    name = prefix + short
    if not short or not NAME_RE.match(name) or len(name) > MAX_NAME or "__" in short:
        raise ActionError(
            "database name may only contain lowercase letters, digits and single underscores"
        )
    return name


def _site(paths: settings.Paths, domain: object) -> tuple[list[Site], Site]:
    sites = websites.load_sites(paths)
    name = validate.domain(domain)
    site = next((s for s in sites if s.domain == name), None)
    if site is None:
        raise ActionError(f"no website {name}")
    return sites, site


def _owned(site: Site, value: object) -> str:
    name = full_name(site, value)
    if name not in site.databases:
        raise ActionError(f"no database {name} on {site.domain}")
    return name


def _describe(site: Site, name: str, sizes: dict[str, int]) -> dict[str, Any]:
    return {"name": name, "user": name, "domain": site.domain, "size": sizes.get(name, 0)}


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@action("database.list")
def list_databases(params: dict[str, Any]) -> list[dict[str, Any]]:
    paths = settings.load()
    sites = websites.load_sites(paths)
    if params.get("domain") is not None:
        wanted = validate.domain(params["domain"])
        sites = [s for s in sites if s.domain == wanted]
    names = [n for s in sites for n in s.databases]
    sizes = server(paths).sizes(names) if names else {}
    return [_describe(s, n, sizes) for s in sites for n in sorted(s.databases)]


@action("database.create")
def create_database(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    password = _password(params.get("password"))
    with websites._lock:
        _sites, site = _site(paths, params.get("domain"))
        name = full_name(site, params.get("name"))
        db = server(paths)
        if name in site.databases or db.database_exists(name):
            raise ActionError(f"the database {name} already exists")
        if db.user_exists(name):
            raise ActionError(f"a database user {name} already exists")
        db.create_database(name, password)
        site.databases.append(name)
        try:
            websites._save_site(paths, site)
        except Exception:
            db.drop_database(name)
            raise
    result = _describe(site, name, {})
    result["password"] = password
    return result


@action("database.password")
def change_password(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    password = _password(params.get("password"))
    _sites, site = _site(paths, params.get("domain"))
    name = _owned(site, params.get("name"))
    server(paths).set_password(name, password)
    return {"name": name, "user": name, "domain": site.domain, "password": password}


@action("database.delete")
def delete_database(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    with websites._lock:
        _sites, site = _site(paths, params.get("domain"))
        name = _owned(site, params.get("name"))
        server(paths).drop_database(name)
        site.databases.remove(name)
        websites._save_site(paths, site)
    return {"name": name, "domain": site.domain}


def drop_all(paths: settings.Paths, site: Site) -> None:
    """Remove every database of a website (used when the website is deleted)."""
    db = server(paths)
    for name in list(site.databases):
        db.drop_database(name)
        site.databases.remove(name)


# ---------------------------------------------------------------------------
# phpMyAdmin single sign-on
#
# The panel asks for a sign-on link. The agent creates a temporary MariaDB user
# that can only reach this website's databases, and writes its credentials to a
# one-time token file that only phpMyAdmin's PHP user can read. tela-signon.php
# reads and deletes the file within a minute, logs in, and the user never sees
# a password. Temporary users are dropped after SSO_USER_HOURS.
# ---------------------------------------------------------------------------


def _load_sso_users(paths: settings.Paths) -> dict[str, int]:
    try:
        return {str(k): int(v) for k, v in json.loads(paths.sso_users_file.read_text()).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_sso_users(paths: settings.Paths, users: dict[str, int]) -> None:
    sysops.write_atomic(paths.sso_users_file, json.dumps(users, indent=2) + "\n", 0o600)


def expire_sso_users(paths: settings.Paths, db: MariaDB | None = None) -> int:
    users = _load_sso_users(paths)
    cutoff = int(time.time()) - SSO_USER_HOURS * 3600
    expired = [u for u, created in users.items() if created < cutoff]
    if expired:
        db = db or server(paths)
        for user in expired:
            db.drop_user(user)
            users.pop(user)
        _save_sso_users(paths, users)
    # Token files nobody used.
    if paths.pma_tokens.exists():
        for token in paths.pma_tokens.glob("*.json"):
            if token.stat().st_mtime < time.time() - SSO_TOKEN_SECONDS * 5:
                token.unlink(missing_ok=True)
    return len(expired)


@action("phpmyadmin.signon")
def phpmyadmin_signon(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    if not paths.pma_installed:
        raise ActionError("phpMyAdmin is not installed. Run the Tela installer again.")
    _sites, site = _site(paths, params.get("domain"))
    if not site.databases:
        raise ActionError(f"{site.domain} has no databases yet")
    focus = _owned(site, params["name"]) if params.get("name") else None

    db = server(paths)
    with websites._lock:
        expire_sso_users(paths, db)
        user = "tela_sso_" + secrets.token_hex(6)
        password = generate_password(32)
        db.create_temp_user(user, password, site.databases)
        users = _load_sso_users(paths)
        users[user] = int(time.time())
        _save_sso_users(paths, users)

    token = secrets.token_urlsafe(32)
    payload = {
        "user": user,
        "password": password,
        "expires": int(time.time()) + SSO_TOKEN_SECONDS,
        "db": focus,
    }
    token_file = paths.pma_tokens / f"{token}.json"
    try:
        paths.pma_tokens.mkdir(parents=True, exist_ok=True)
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        if os.geteuid() == 0:
            shutil.chown(token_file, user=paths.pma_user, group=paths.pma_user)
    except (OSError, LookupError) as exc:
        token_file.unlink(missing_ok=True)
        db.drop_user(user)
        raise ActionError(f"could not prepare the phpMyAdmin sign-on: {exc}") from exc
    return {"port": paths.pma_port, "path": f"/tela-signon.php?token={token}"}
