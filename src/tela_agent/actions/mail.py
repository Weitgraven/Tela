"""Email: mail domains, mailboxes, aliases, DKIM and the mail server's hostname.

State lives in <mail_dir>/settings.json and <mail_dir>/domains/<domain>.json.
Every change re-renders Postfix's lookup tables and Dovecot's user file from
that state (see tela_agent.mailconf), then reloads what needs reloading.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import string
import threading
import time
import types
from pathlib import Path
from typing import Any

from .. import dnsrecords, mailconf, settings, sysops, validate
from ..mailsys import MailSystem, postfix_settings, rspamd_files
from ..registry import ActionError, action
from ..sha512crypt import sha512_crypt
from . import dns as dns_actions
from . import websites

_lock = threading.RLock()
SELECTOR = "tela"
_LOCAL = re.compile(r"^(?!\.)(?!.*\.\.)[a-z0-9._+-]{1,64}(?<!\.)$")
PASSWORD_ALPHABET = string.ascii_letters + string.digits
MAX_QUOTA_MB = 1024 * 1024


def system() -> MailSystem:
    """Replaced by a fake in tests."""
    return MailSystem()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _settings_file(paths: settings.Paths) -> Path:
    return paths.mail_dir / "settings.json"


def _domain_file(paths: settings.Paths, domain: str) -> Path:
    return paths.mail_dir / "domains" / f"{domain}.json"


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sysops.write_atomic(path, json.dumps(data, indent=2) + "\n", 0o600)


def load_settings(paths: settings.Paths) -> dict[str, Any]:
    defaults: dict[str, Any] = {"hostname": "", "ssl_cert": "", "ssl_key": ""}
    try:
        return {**defaults, **json.loads(_settings_file(paths).read_text())}
    except (OSError, ValueError):
        return defaults


def load_domains(paths: settings.Paths) -> list[dict[str, Any]]:
    folder = paths.mail_dir / "domains"
    if not folder.exists():
        return []
    return [json.loads(f.read_text()) for f in sorted(folder.glob("*.json"))]


def _load_domain(paths: settings.Paths, domain: object) -> dict[str, Any]:
    name = validate.domain(domain)
    try:
        return json.loads(_domain_file(paths, name).read_text())
    except OSError as exc:
        raise ActionError(f"{name} isn't set up for email") from exc


def _apply(paths: settings.Paths, domains: list[dict[str, Any]] | None = None) -> None:
    """Write Postfix's tables and Dovecot's users from the saved state and reload."""
    mail = system()
    if not mail.installed:
        raise ActionError("the mail server isn't installed; run the Tela installer again")
    domains = load_domains(paths) if domains is None else domains
    uid, gid = mail.vmail_ids()
    for name, content in mailconf.postfix_maps(domains).items():
        mail.write(mail.postfix_dir / name, content, 0o644)
    users = mailconf.dovecot_users(domains, uid, gid, mail.dovecot_version(), mail.vmail_root)
    mail.write_users(users)
    mail.reload_postfix()


def _public(mailbox: dict[str, Any], domain: str, usage: int | None = None) -> dict[str, Any]:
    return {
        "local": mailbox["local"],
        "address": mailconf.address(mailbox["local"], domain),
        "name": mailbox.get("name", ""),
        "quota_mb": mailbox.get("quota_mb", 0),
        "enabled": mailbox.get("enabled", True),
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _local(value: object, allow_catchall: bool = False) -> str:
    if not isinstance(value, str):
        raise ActionError("address must be a string")
    local = value.strip().lower()
    if allow_catchall and local == "*":
        return local
    if not _LOCAL.match(local):
        raise ActionError("the part before @ may contain letters, digits, '.', '_', '+' and '-'")
    return local


def _password(value: object) -> tuple[str, bool]:
    """Return (password, generated)."""
    if value in (None, ""):
        return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(20)), True
    if not isinstance(value, str) or not 10 <= len(value) <= 128:
        raise ActionError("password must be 10 to 128 characters")
    if any(ord(c) < 32 for c in value) or ":" in value:
        raise ActionError("password can't contain ':' or control characters")
    return value, False


def _quota(value: object) -> int:
    if value in (None, ""):
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_QUOTA_MB:
        raise ActionError("quota must be a number of megabytes (0 for unlimited)")
    return value


def _destinations(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ActionError("give 1 to 50 destination addresses")
    result = []
    for item in value:
        addr = validate.email(item)
        if not addr:
            raise ActionError("destination addresses can't be empty")
        if addr not in result:
            result.append(addr)
    return result


def _display_name(value: object) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 100 or any(c in value for c in ":\n\r"):
        raise ActionError("invalid display name")
    return value.strip()


# ---------------------------------------------------------------------------
# Server setup (run by the installer through tela_agent.mailsetup)
# ---------------------------------------------------------------------------


def setup_server(paths: settings.Paths) -> str:
    """(Re)write the full mail server configuration. Safe to run repeatedly."""
    mail = system()
    conf = load_settings(paths)
    hostname = conf["hostname"] or _default_hostname()
    if not conf["hostname"]:
        conf["hostname"] = hostname
        _save(_settings_file(paths), conf)
    cert, key = (
        (Path(conf["ssl_cert"]), Path(conf["ssl_key"]))
        if conf["ssl_cert"] and Path(conf["ssl_cert"]).exists()
        else (paths.panel_cert, paths.panel_key)
    )
    mail.use_certificate(cert, key)
    _prepare_mailhost(paths)
    mail.postfix_dir.mkdir(parents=True, exist_ok=True)
    mail.dovecot_users.parent.mkdir(parents=True, exist_ok=True)
    version = mail.dovecot_version()
    uid, _gid = mail.vmail_ids()
    mail.write(mail.dovecot_conf, mailconf.dovecot_conf(
        version, users_file=mail.dovecot_users, cert=mail.cert, key=mail.key, uid=uid,
        vmail_root=mail.vmail_root, ipv6=mail.ipv6_available(),
    ), 0o644)  # fmt: skip
    for name, content in rspamd_files(mail.dkim_dir).items():
        mail.write(mail.rspamd_local / name, content, 0o644)
    mail.postconf(postfix_settings(hostname))
    mail.configure_submission()
    _apply(paths)
    mail.check_dovecot()
    return f"mail server configured for {hostname} (Dovecot {version[0]}.{version[1]})"


PLACEHOLDER = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Mail server</title>
<body style="font-family:sans-serif;margin:4rem auto;max-width:32rem;color:#333">
<h1>Mail server</h1><p>This server handles email. Webmail is not installed.</p>
</body></html>
"""


def _prepare_mailhost(paths: settings.Paths) -> None:
    """Directories the mail host name's virtual host serves from."""
    root = paths.mailhost_root
    (root / ".acme" / ".well-known" / "acme-challenge").mkdir(parents=True, exist_ok=True)
    (root / "public").mkdir(parents=True, exist_ok=True)
    for path in (root, root / ".acme", root / ".acme" / ".well-known",
                 root / ".acme" / ".well-known" / "acme-challenge", root / "public"):  # fmt: skip
        path.chmod(0o755)
    index = root / "public" / "index.html"
    if not index.exists():
        sysops.write_atomic(index, PLACEHOLDER, 0o644)


def _default_hostname() -> str:
    import socket

    name = socket.getfqdn()
    return name if name.count(".") >= 2 else f"mail.{name}" if "." in name else "mail.localdomain"


# ---------------------------------------------------------------------------
# Mail hostname and its certificate
# ---------------------------------------------------------------------------


@action("mail.status")
def status(_params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    conf = load_settings(paths)
    mail = system()
    expires = None
    if conf["ssl_cert"] and Path(conf["ssl_cert"]).exists():
        expires = sysops.system.cert_expiry(Path(conf["ssl_cert"]))
    return {
        "installed": mail.installed,
        "hostname": conf["hostname"],
        "ssl_expires_at": expires,
        "ssl_issued": bool(conf["ssl_cert"]),
        "webmail_installed": paths.webmail_installed,
        "webmail_url": f"https://{conf['hostname']}/" if conf["hostname"] else "",
    }


def _rewrite_web_server(paths: settings.Paths) -> None:
    """The mail hostname is a virtual host too (certificate checks and webmail)."""
    websites._apply(paths, websites.load_sites(paths))


@action("mail.settings.update")
def update_settings(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    hostname = validate.domain(params.get("hostname"))
    if hostname.count(".") < 2:
        raise ActionError("use a host name like mail.example.com, not the domain itself")
    if any(hostname in s.all_names for s in websites.load_sites(paths)):
        raise ActionError(f"{hostname} is already used by a website")
    with _lock, websites._lock:
        conf = load_settings(paths)
        previous = dict(conf)
        if conf["hostname"] != hostname:
            conf.update(hostname=hostname, ssl_cert="", ssl_key="")
        _save(_settings_file(paths), conf)
        try:
            mail = system()
            mail.postconf(postfix_settings(hostname))
            if not conf["ssl_cert"]:
                mail.use_certificate(paths.panel_cert, paths.panel_key)
            mail.reload_postfix()
            mail.reload_dovecot()
            _rewrite_web_server(paths)
        except Exception:
            _save(_settings_file(paths), previous)
            raise
    return status({})


@action("mail.hostname_ssl")
def issue_hostname_certificate(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    email = validate.email(params.get("email"))
    with _lock, websites._lock:
        conf = load_settings(paths)
        hostname = conf["hostname"]
        if not hostname:
            raise ActionError("set the mail server's host name first")
        if not sysops.system.resolve(hostname):
            raise ActionError(
                f"{hostname} does not resolve yet. Point its A record at this server first."
            )
        acme = paths.mailhost_root / ".acme"
        (acme / ".well-known" / "acme-challenge").mkdir(parents=True, exist_ok=True)
        target = types.SimpleNamespace(domain=hostname, acme_dir=acme)
        websites._reachable_names(target, [hostname])  # raises with a clear message
        sysops.system.certbot_issue("tela-mail", [hostname], acme, email)
        live = paths.letsencrypt / "live" / "tela-mail"
        conf.update(ssl_cert=str(live / "fullchain.pem"), ssl_key=str(live / "privkey.pem"))
        _save(_settings_file(paths), conf)
        mail = system()
        mail.use_certificate(Path(conf["ssl_cert"]), Path(conf["ssl_key"]))
        mail.reload_postfix()
        mail.restart_dovecot()
        _rewrite_web_server(paths)
    return status({})


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------


@action("mail.domain.list")
def list_domains(_params: dict[str, Any]) -> list[dict[str, Any]]:
    paths = settings.load()
    return [
        {"domain": d["domain"], "mailboxes": len(d["mailboxes"]), "aliases": len(d["aliases"])}
        for d in load_domains(paths)
    ]


@action("mail.domain.create")
def create_domain(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    name = validate.domain(params.get("domain"))
    with _lock:
        if _domain_file(paths, name).exists():
            raise ActionError(f"{name} is already set up for email")
        conf = load_settings(paths)
        if name == conf["hostname"]:
            raise ActionError(f"{name} is the mail server's own host name")
        mail = system()
        mail.create_dkim_key(name, SELECTOR)
        domain = {"domain": name, "dkim_selector": SELECTOR, "mailboxes": [], "aliases": [],
                  "created_at": int(time.time())}  # fmt: skip
        _save(_domain_file(paths, name), domain)
        try:
            _apply(paths)
        except Exception:
            _domain_file(paths, name).unlink(missing_ok=True)
            raise
    result = get_domain({"domain": name})
    if validate.boolean(params.get("dns"), default=True):
        result["dns"] = _apply_dns(paths, result)
    return result


@action("mail.domain.get")
def get_domain(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    domain = _load_domain(paths, params.get("domain"))
    mail = system()
    name = domain["domain"]
    conf = load_settings(paths)
    try:
        public_key = mail.dkim_public_key(name, domain["dkim_selector"])
    except ActionError:
        public_key = ""
    dns_conf = dns_actions.load_settings(paths)
    records = mailconf.recommended_records(
        name, conf["hostname"] or f"mail.{name}", domain["dkim_selector"], public_key,
        dns_conf["public_ipv4"], dns_conf["public_ipv6"],
    )  # fmt: skip
    return {
        "domain": name,
        "mailboxes": [
            _public(m, name, mail.mailbox_usage(name, m["local"]))
            for m in sorted(domain["mailboxes"], key=lambda m: m["local"])
        ],
        "aliases": sorted(domain["aliases"], key=lambda a: (a["local"] == "*", a["local"])),
        "dkim_selector": domain["dkim_selector"],
        "records": records,
        "hostname": conf["hostname"],
        "dns_zone": (dns_actions.zone_for(paths, name) or {}).get("zone"),
    }


@action("mail.domain.delete")
def delete_domain(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    remove_mail = validate.boolean(params.get("remove_mail"), default=True)
    with _lock:
        domain = _load_domain(paths, params.get("domain"))
        name = domain["domain"]
        path = _domain_file(paths, name)
        backup = path.read_text()
        path.unlink()
        try:
            _apply(paths)
        except Exception:
            sysops.write_atomic(path, backup, 0o600)
            raise
        mail = system()
        mail.remove_dkim_key(name, domain["dkim_selector"])
        if remove_mail:
            mail.remove_mail(name)
    return {"domain": name, "mail_removed": remove_mail}


def _apply_dns(paths: settings.Paths, info: dict[str, Any]) -> dict[str, Any]:
    """Add the recommended records to the domain's DNS zone, where missing."""
    zone_name = info.get("dns_zone")
    if not zone_name:
        return {"zone": None, "added": [], "skipped": [], "message": "no DNS zone in Tela"}
    if not info["hostname"]:
        return {"zone": zone_name, "added": [], "skipped": [],
                "message": "set the mail server's host name first"}  # fmt: skip
    added, skipped = [], []
    try:
        zone = dns_actions._load_zone(paths, zone_name)
        existing = dns_actions._records(paths, zone)
        domain = info["domain"]
        for rec in info["records"]:
            full = dnsrecords.fqdn(rec["name"], domain)
            rel = dnsrecords.relative_name(full, zone_name)
            same = [r for r in existing if r["name"] == rel and r["type"] == rec["type"]]
            is_spf = rec["type"] == "TXT" and rec["content"].startswith("v=spf1")
            is_dkim = rec["type"] == "TXT" and rec["content"].startswith("v=DKIM1")
            is_dmarc = rec["type"] == "TXT" and rec["content"].startswith("v=DMARC1")
            clash = (
                (rec["type"] == "MX" and same)
                or (is_spf and any(r["content"].startswith("v=spf1") for r in same))
                or ((is_dkim or is_dmarc) and same)
            )
            label = f"{rec['type']} {full}"
            if clash:
                skipped.append(label)
                continue
            record = dnsrecords.validate({**rec, "name": rel}, zone_name,
                                         cloudflare=zone["provider"] == "cloudflare")  # fmt: skip
            with dns_actions._lock:
                dns_actions._add(paths, zone, record)
            zone = dns_actions._load_zone(paths, zone_name)
            added.append(label)
        # The mail host name itself needs an address.
        dns_actions.ensure_site_records(paths, info["hostname"], [])
    except ActionError as exc:
        return {"zone": zone_name, "added": added, "skipped": skipped, "message": str(exc)}
    return {"zone": zone_name, "added": added, "skipped": skipped, "message": ""}


@action("mail.domain.dns")
def domain_dns(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    info = get_domain(params)
    return _apply_dns(paths, info)


# ---------------------------------------------------------------------------
# Mailboxes
# ---------------------------------------------------------------------------


def _find(domain: dict[str, Any], local: str) -> dict[str, Any] | None:
    return next((m for m in domain["mailboxes"] if m["local"] == local), None)


def _check_free(domain: dict[str, Any], local: str) -> None:
    if _find(domain, local) or any(a["local"] == local for a in domain["aliases"]):
        raise ActionError(f"{local}@{domain['domain']} already exists")


def _update_domain(paths: settings.Paths, domain: dict[str, Any], change) -> Any:
    """Apply `change(domain)`, save and reload; restore the old state on failure."""
    path = _domain_file(paths, domain["domain"])
    backup = path.read_text()
    result = change(domain)
    _save(path, domain)
    try:
        _apply(paths)
    except Exception:
        sysops.write_atomic(path, backup, 0o600)
        with contextlib.suppress(Exception):
            _apply(paths)
        raise
    return result


@action("mail.mailbox.create")
def create_mailbox(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    local = _local(params.get("local"))
    password, generated = _password(params.get("password"))
    quota = _quota(params.get("quota_mb"))
    name = _display_name(params.get("name"))
    with _lock:
        domain = _load_domain(paths, params.get("domain"))
        _check_free(domain, local)
        mailbox = {"local": local, "name": name, "password": sha512_crypt(password),
                   "quota_mb": quota, "enabled": True, "created_at": int(time.time())}  # fmt: skip
        _update_domain(paths, domain, lambda d: d["mailboxes"].append(mailbox))
    result = _public(mailbox, domain["domain"], 0)
    if generated:
        result["password"] = password
    return result


@action("mail.mailbox.update")
def update_mailbox(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    local = _local(params.get("local"))
    new_password = None
    if "password" in params:
        new_password, _generated = _password(params.get("password"))
    with _lock:
        domain = _load_domain(paths, params.get("domain"))
        mailbox = _find(domain, local)
        if mailbox is None:
            raise ActionError(f"no mailbox {local}@{domain['domain']}")

        def change(d: dict[str, Any]) -> None:
            m = _find(d, local)
            assert m is not None
            if new_password is not None:
                m["password"] = sha512_crypt(new_password)
            if "quota_mb" in params:
                m["quota_mb"] = _quota(params["quota_mb"])
            if "enabled" in params:
                m["enabled"] = validate.boolean(params["enabled"])
            if "name" in params:
                m["name"] = _display_name(params["name"])

        _update_domain(paths, domain, change)
        mailbox = _find(domain, local)
    assert mailbox is not None
    result = _public(mailbox, domain["domain"])
    if new_password is not None and params.get("password") in (None, ""):
        result["password"] = new_password
    return result


@action("mail.mailbox.delete")
def delete_mailbox(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    local = _local(params.get("local"))
    remove_mail = validate.boolean(params.get("remove_mail"), default=True)
    with _lock:
        domain = _load_domain(paths, params.get("domain"))
        if _find(domain, local) is None:
            raise ActionError(f"no mailbox {local}@{domain['domain']}")
        _update_domain(paths, domain, lambda d: d["mailboxes"].remove(_find(d, local)))
        if remove_mail:
            system().remove_mail(domain["domain"], local)
    return {"address": mailconf.address(local, domain["domain"]), "mail_removed": remove_mail}


# ---------------------------------------------------------------------------
# Aliases (forwarders) and catch-all
# ---------------------------------------------------------------------------


@action("mail.alias.create")
def create_alias(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    local = _local(params.get("local"), allow_catchall=True)
    destinations = _destinations(params.get("destinations"))
    with _lock:
        domain = _load_domain(paths, params.get("domain"))
        if local == "*":
            if any(a["local"] == "*" for a in domain["aliases"]):
                raise ActionError("this domain already has a catch-all address")
        else:
            _check_free(domain, local)
        if mailconf.address(local, domain["domain"]) in destinations:
            raise ActionError("an alias can't forward to itself")
        alias = {"local": local, "destinations": destinations}
        _update_domain(paths, domain, lambda d: d["aliases"].append(alias))
    return alias


@action("mail.alias.delete")
def delete_alias(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    local = _local(params.get("local"), allow_catchall=True)
    with _lock:
        domain = _load_domain(paths, params.get("domain"))
        if not any(a["local"] == local for a in domain["aliases"]):
            raise ActionError("no such alias")
        _update_domain(paths, domain,
                       lambda d: d.update(aliases=[a for a in d["aliases"]
                                                   if a["local"] != local]))  # fmt: skip
    return {"local": local}
