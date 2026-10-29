"""Websites: one OpenLiteSpeed virtual host, Linux user and home directory per site."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .. import ols, settings, sysops, validate
from ..ols import Site
from ..registry import ActionError, action

# One site operation at a time: they all rewrite the shared web server config.
_lock = threading.Lock()

RESERVED_USERS = {
    "root", "admin", "tela", "www", "www-data", "nobody", "nogroup", "lsadm", "mysql",
    "postfix", "dovecot", "vmail", "mail", "ftp", "sshd", "daemon", "bin", "sys", "backup",
}  # fmt: skip

DEFAULT_INDEX = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{domain}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;margin:0;display:grid;place-items:center;
min-height:100vh;background:#edf0f2;color:#1c2340}}main{{max-width:32rem;padding:2rem}}
h1{{font-size:1.75rem;margin:0 0 .5rem}}p{{margin:0;color:#5a6380}}</style></head>
<body><main><h1>{domain}</h1>
<p>This website is ready. Upload your files to <code>public_html</code> to replace this page.</p>
</main></body></html>
"""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _paths() -> settings.Paths:
    return settings.load()


def _state_file(paths: settings.Paths, domain: str):
    return paths.state_dir / f"{domain}.json"


def load_sites(paths: settings.Paths) -> list[Site]:
    if not paths.state_dir.exists():
        return []
    sites = []
    for file in sorted(paths.state_dir.glob("*.json")):
        sites.append(Site.from_dict(json.loads(file.read_text())))
    return sites


def _load_site(paths: settings.Paths, domain: str) -> Site:
    file = _state_file(paths, domain)
    if not file.exists():
        raise ActionError(f"no website {domain}")
    return Site.from_dict(json.loads(file.read_text()))


def _save_site(paths: settings.Paths, site: Site) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    sysops.write_atomic(
        _state_file(paths, site.domain), json.dumps(site.to_dict(), indent=2) + "\n", 0o600
    )


def _apply(paths: settings.Paths, sites: list[Site], removed: list[str] | None = None) -> None:
    """Write the web server config and reload; restore the previous config on failure."""
    removed = removed or []
    domains = [s.domain for s in sites] + removed
    touched = [paths.vhost_conf_root / d / "vhconf.conf" for d in domains]
    touched += [paths.tela_conf_dir / f"vhost-{d}.conf" for d in domains]
    before = ols.snapshot(paths, touched)
    try:
        ols.write(sites, paths, removed)
        sysops.system.reload_web_server(paths.ols_root)
    except Exception:
        ols.restore(before)
        with contextlib.suppress(Exception):
            sysops.system.reload_web_server(paths.ols_root)
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _username_for(domain: str) -> str:
    labels = domain.split(".")[:-1] or domain.split(".")
    base = re.sub(r"[^a-z0-9]", "", "".join(labels))[:24] or "site"
    if not base[0].isalpha():
        base = "s" + base[:23]
    candidate, n = base, 1
    while candidate in RESERVED_USERS or sysops.system.user_exists(candidate):
        n += 1
        candidate = f"{base[: 24 - len(str(n))]}{n}"
    return candidate


def _check_names_free(sites: list[Site], names: list[str], exclude: str | None = None) -> None:
    taken = {n: s.domain for s in sites if s.domain != exclude for n in s.all_names}
    mailhost = ols.mail_settings(_paths()).get("hostname")
    for name in names:
        if name == mailhost:
            raise ActionError(f"{name} is the mail server's host name (webmail)")
        if name in taken:
            raise ActionError(f"{name} is already used by the website {taken[name]}")


def _check_php(paths: settings.Paths, version: str) -> None:
    installed = sysops.system.php_versions(paths.ols_root)
    if version not in installed:
        raise ActionError(
            f"PHP {version} is not installed (installed: {', '.join(installed) or 'none'})"
        )


def _prepare_dirs(paths: settings.Paths, site: Site) -> None:
    sys_ = sysops.system
    ols_user, ols_group = sys_.ols_group(paths.httpd_config)
    public = site.home / "public_html"
    logs = site.home / "logs"
    challenge = site.acme_dir / ".well-known" / "acme-challenge"

    site.home.mkdir(parents=True, exist_ok=True)
    public.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    challenge.mkdir(parents=True, exist_ok=True)

    os.chmod(site.home, 0o711)
    sys_.chown(site.home, site.user, site.user)
    os.chmod(public, 0o750)
    sys_.chown(public, site.user, ols_group)
    os.chmod(logs, 0o750)
    sys_.chown(logs, ols_user, site.user)
    for d in (site.acme_dir, site.acme_dir / ".well-known", challenge):
        os.chmod(d, 0o750)
        sys_.chown(d, "root", ols_group)

    if not any(public.iterdir()):
        index = public / "index.html"
        index.write_text(DEFAULT_INDEX.format(domain=site.domain))
        os.chmod(index, 0o644)
        sys_.chown(index, site.user, site.user)


def _live_ssl(_paths: settings.Paths, site: Site) -> dict[str, Any]:
    data = site.to_dict()
    if site.ssl_cert and Path(site.ssl_cert).exists():
        data["ssl_expires_at"] = sysops.system.cert_expiry(Path(site.ssl_cert))
    return data


# Cloudflare's published ranges; a proxied ("orange cloud") record resolves here.
CLOUDFLARE_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
        "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
        "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
        "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22", "2400:cb00::/32",
        "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
        "2a06:98c0::/29", "2c0f:f248::/32",
    )
]  # fmt: skip


def _behind_cloudflare(ips: set[str]) -> bool:
    for ip in ips:
        with contextlib.suppress(ValueError):
            addr = ipaddress.ip_address(ip)
            if any(addr in net for net in CLOUDFLARE_NETS):
                return True
    return False


def _reachable_names(site: Site, candidates: list[str]) -> list[str]:
    """Check, before asking Let's Encrypt, that each name really reaches this site.

    Writes a test file where certbot will put its challenge and fetches it, first
    from the web server directly, then through each name's public DNS. Returns
    the names that work; raises if the main domain doesn't.
    """
    challenge_dir = site.acme_dir / ".well-known" / "acme-challenge"
    token = "tela-check-" + secrets.token_urlsafe(16)
    probe = challenge_dir / token
    probe.write_text(token)
    os.chmod(probe, 0o644)
    path = f"/.well-known/acme-challenge/{token}"
    try:
        status, body = sysops.system.http_fetch(f"http://127.0.0.1{path}", host=site.domain)
        if body.strip() != token:
            raise ActionError(
                f"the web server on this machine doesn't serve {site.domain}'s validation "
                f"file (HTTP {status or 'no response'}). Check that the web server is running."
            )
        reachable = []
        for name in candidates:
            status, body = sysops.system.http_fetch(f"http://{name}{path}")
            if body.strip() == token:
                reachable.append(name)
            elif name == site.domain:
                raise ActionError(_unreachable_message(name, status))
        return reachable
    finally:
        probe.unlink(missing_ok=True)


def _unreachable_message(name: str, status: int) -> str:
    got = f"got HTTP {status}" if status else "no response"
    message = f"http://{name} doesn't reach this server ({got} for a test file)."
    if _behind_cloudflare(sysops.system.resolve(name)):
        message += (
            " The domain is behind Cloudflare's proxy. Either make sure its DNS record in "
            "Cloudflare points to this server's IP address, or switch the record to "
            "'DNS only' (grey cloud) while the certificate is issued."
        )
    else:
        message += " Point the domain's A (and AAAA) record at this server's IP address."
    return message


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@action("php.versions")
def php_versions(_params: dict[str, Any]) -> list[str]:
    return sysops.system.php_versions(_paths().ols_root)


@action("website.list")
def list_sites(_params: dict[str, Any]) -> list[dict[str, Any]]:
    paths = _paths()
    return [_live_ssl(paths, s) for s in load_sites(paths)]


@action("website.get")
def get_site(params: dict[str, Any]) -> dict[str, Any]:
    paths = _paths()
    return _live_ssl(paths, _load_site(paths, validate.domain(params.get("domain"))))


@action("website.create")
def create_site(params: dict[str, Any]) -> dict[str, Any]:
    paths = _paths()
    domain = validate.domain(params.get("domain"))
    php = validate.php_version(params.get("php_version"))
    aliases = validate.aliases(params.get("aliases"), domain)
    if validate.boolean(params.get("include_www"), default=True) and f"www.{domain}" not in aliases:
        aliases.insert(0, f"www.{domain}")

    with _lock:
        sites = load_sites(paths)
        if any(s.domain == domain for s in sites):
            raise ActionError(f"the website {domain} already exists")
        _check_names_free(sites, [domain, *aliases])
        _check_php(paths, php)

        user = _username_for(domain)
        site = Site(
            domain=domain,
            user=user,
            php_version=php,
            home=paths.sites_root / domain,
            aliases=aliases,
            created_at=int(time.time()),
        )
        if site.home.exists() and any(site.home.iterdir()):
            raise ActionError(f"{site.home} already exists and is not empty")

        sysops.system.create_site_user(user, site.home)
        try:
            _prepare_dirs(paths, site)
            _save_site(paths, site)
            _apply(paths, [*sites, site])
        except Exception:
            _state_file(paths, domain).unlink(missing_ok=True)
            sysops.system.remove_tree(site.home)
            sysops.system.delete_site_user(user)
            raise
    from .dns import ensure_site_records

    result = site.to_dict()
    result["dns"] = ensure_site_records(paths, site.domain, site.aliases)
    return result


@action("website.update")
def update_site(params: dict[str, Any]) -> dict[str, Any]:
    paths = _paths()
    domain = validate.domain(params.get("domain"))
    with _lock:
        sites = load_sites(paths)
        site = next((s for s in sites if s.domain == domain), None)
        if site is None:
            raise ActionError(f"no website {domain}")
        original = Site.from_dict(site.to_dict())

        if "php_version" in params:
            site.php_version = validate.php_version(params["php_version"])
            _check_php(paths, site.php_version)
        if "aliases" in params:
            site.aliases = validate.aliases(params["aliases"], domain)
            _check_names_free(sites, site.aliases, exclude=domain)
        if "force_https" in params:
            site.force_https = validate.boolean(params["force_https"])
            if site.force_https and not site.ssl_cert:
                raise ActionError("issue an SSL certificate before redirecting to HTTPS")

        _save_site(paths, site)
        try:
            _apply(paths, sites)
        except Exception:
            _save_site(paths, original)
            raise
    return _live_ssl(paths, site)


@action("website.delete")
def delete_site(params: dict[str, Any]) -> dict[str, Any]:
    paths = _paths()
    domain = validate.domain(params.get("domain"))
    remove_files = validate.boolean(params.get("remove_files"), default=True)
    remove_databases = validate.boolean(params.get("remove_databases"), default=remove_files)
    with _lock:
        sites = load_sites(paths)
        site = next((s for s in sites if s.domain == domain), None)
        if site is None:
            raise ActionError(f"no website {domain}")
        kept_databases = [] if remove_databases else list(site.databases)
        if remove_databases and site.databases:
            from .databases import drop_all

            drop_all(paths, site)
            _save_site(paths, site)
        remaining = [s for s in sites if s.domain != domain]
        _apply(paths, remaining, removed=[domain])
        _state_file(paths, domain).unlink(missing_ok=True)
        if site.ssl_cert:
            sysops.system.certbot_delete(domain)
        if remove_files:
            sysops.system.remove_tree(site.home)
            sysops.system.delete_site_user(site.user)
    return {"domain": domain, "files_removed": remove_files, "kept_databases": kept_databases}


@action("website.ssl_issue")
def issue_ssl(params: dict[str, Any]) -> dict[str, Any]:
    paths = _paths()
    domain = validate.domain(params.get("domain"))
    email = validate.email(params.get("email"))
    with _lock:
        sites = load_sites(paths)
        site = next((s for s in sites if s.domain == domain), None)
        if site is None:
            raise ActionError(f"no website {domain}")

        if not sysops.system.resolve(domain):
            raise ActionError(
                f"{domain} does not resolve yet. Point its DNS A record at this server first."
            )
        _prepare_dirs(paths, site)
        # Wildcards need DNS validation and are left out.
        candidates = [domain] + [a for a in site.aliases if not a.startswith("*.")]
        names = _reachable_names(site, candidates)
        sysops.system.certbot_issue(domain, names, site.acme_dir, email)

        live = paths.letsencrypt / "live" / domain
        original = Site.from_dict(site.to_dict())
        site.ssl_cert = str(live / "fullchain.pem")
        site.ssl_key = str(live / "privkey.pem")
        site.ssl_names = names
        site.ssl_expires_at = sysops.system.cert_expiry(live / "fullchain.pem")
        _save_site(paths, site)
        try:
            _apply(paths, sites)
        except Exception:
            _save_site(paths, original)
            raise
    result = site.to_dict()
    result["skipped_names"] = [a for a in site.aliases if a not in names]
    return result


# ---------------------------------------------------------------------------
# .htaccess changes
# ---------------------------------------------------------------------------


class HtaccessWatch:
    """OpenLiteSpeed reads a site's top-level .htaccess only when it loads its
    configuration, so rules WordPress or a plugin writes there (permalinks,
    caching, redirects) do nothing until the next reload. The agent checks
    every half minute and reloads the web server when one changed."""

    def __init__(self) -> None:
        self.seen: dict[str, tuple[int, int] | None] | None = None

    def snapshot(self, paths: settings.Paths) -> dict[str, tuple[int, int] | None]:
        result: dict[str, tuple[int, int] | None] = {}
        for site in load_sites(paths):
            path = site.home / "public_html" / ".htaccess"
            try:
                st = path.stat()
                result[site.domain] = (st.st_mtime_ns, st.st_size)
            except OSError:
                result[site.domain] = None
        return result

    def check(self, paths: settings.Paths) -> bool:
        """Reload the web server if an .htaccess changed since the last check."""
        current = self.snapshot(paths)
        previous, self.seen = self.seen, current
        if previous is None:
            return False
        changed = [d for d, v in current.items() if d in previous and previous[d] != v]
        if not changed:
            return False
        with _lock:
            sysops.system.reload_web_server(paths.ols_root)
        return True
