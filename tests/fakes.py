"""A fake host system for agent tests: records calls instead of changing the machine."""

from __future__ import annotations

import shutil
from pathlib import Path

from tela_agent.registry import ActionError
from tela_agent.sysops import System


class FakeSystem(System):
    def __init__(self, php: list[str] | None = None) -> None:
        self.users: set[str] = set()
        self.reloads = 0
        self.fail_reload = False
        self.php = php if php is not None else ["8.2", "8.3"]
        self.dns: dict[str, set[str]] = {}
        self.issued: list[tuple[str, list[str], Path]] = []
        self.deleted_certs: list[str] = []
        self.letsencrypt: Path | None = None
        self.sites_root: Path | None = None
        self.elsewhere: set[str] = set()
        self.local_broken = False

    def user_exists(self, name: str) -> bool:
        return name in self.users

    def create_site_user(self, name: str, home: Path) -> None:
        self.users.add(name)

    def delete_site_user(self, name: str) -> None:
        self.users.discard(name)

    def chown(self, path: Path, user: str, group: str) -> None:
        pass

    def remove_tree(self, path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)

    def reload_web_server(self, ols_root: Path) -> None:
        if self.fail_reload:
            raise ActionError("web server is not running after the configuration change")
        self.reloads += 1

    def php_versions(self, ols_root: Path) -> list[str]:
        return list(self.php)

    def resolve(self, name: str) -> set[str]:
        return self.dns.get(name, set())

    def http_fetch(self, url: str, host: str | None = None, timeout: float = 10):
        """Serve challenge files for names that 'point here' (are in self.dns and not
        listed in self.elsewhere), like the real server would."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        name = host or parsed.hostname
        if host is None and (name not in self.dns or name in self.elsewhere):
            return 404, ""
        if self.local_broken:
            return 404, ""
        token = parsed.path.rsplit("/", 1)[-1]
        assert self.sites_root is not None
        for site_dir in self.sites_root.iterdir():
            f = site_dir / ".acme" / ".well-known" / "acme-challenge" / token
            if f.exists():
                return 200, f.read_text()
        return 404, ""

    def certbot_issue(self, cert_name, names, webroot, email) -> None:
        self.issued.append((cert_name, list(names), webroot))
        assert self.letsencrypt is not None
        live = self.letsencrypt / "live" / cert_name
        live.mkdir(parents=True, exist_ok=True)
        (live / "fullchain.pem").write_text("cert")
        (live / "privkey.pem").write_text("key")

    def certbot_delete(self, cert_name: str) -> None:
        self.deleted_certs.append(cert_name)

    def cert_expiry(self, cert: Path) -> int | None:
        return 1_900_000_000


class FakeMariaDB:
    """In-memory stand-in for tela_agent.mariadb.MariaDB."""

    def __init__(self) -> None:
        self.databases: dict[str, int] = {}
        self.users: dict[str, tuple[str, list[str]]] = {}

    def database_exists(self, name: str) -> bool:
        return name in self.databases

    def user_exists(self, user: str) -> bool:
        return user in self.users

    def create_database(self, name: str, password: str) -> None:
        self.databases[name] = 0
        self.users[name] = (password, [name])

    def drop_database(self, name: str) -> None:
        self.databases.pop(name, None)
        self.users.pop(name, None)

    def set_password(self, user: str, password: str) -> None:
        self.users[user] = (password, self.users[user][1])

    def sizes(self, names: list[str]) -> dict[str, int]:
        return {n: self.databases[n] for n in names if n in self.databases}

    def create_temp_user(self, user: str, password: str, databases: list[str]) -> None:
        self.users[user] = (password, list(databases))

    def drop_user(self, user: str) -> None:
        self.users.pop(user, None)


class FakeCloudflare:
    """In-memory Cloudflare account. Records are stored in Tela's own format."""

    def __init__(self) -> None:
        self.tokens = {"good-token": "active"}
        self.zone_list = [
            {"id": "a" * 32, "name": "example.com", "status": "active"},
            {"id": "b" * 32, "name": "cvgalerij.nl", "status": "active"},
        ]
        self.data: dict[str, list[dict]] = {z["id"]: [] for z in self.zone_list}
        self.token = ""

    def __call__(self, token: str):
        self.token = token
        return self

    def verify(self) -> str:
        from tela_agent.registry import ActionError

        if self.token not in self.tokens:
            raise ActionError("Cloudflare: Invalid API Token")
        return self.tokens[self.token]

    def zones(self):
        self.verify()
        return [dict(z) for z in self.zone_list]

    def records(self, zone_id, zone):
        return [dict(r) for r in self.data[zone_id]]

    def create(self, zone_id, zone, record):
        import secrets

        stored = {**record, "id": secrets.token_hex(16)}
        self.data[zone_id].append(stored)
        return dict(stored)

    def update(self, zone_id, zone, record):
        rows = self.data[zone_id]
        for i, r in enumerate(rows):
            if r["id"] == record["id"]:
                rows[i] = dict(record)
                return dict(record)
        from tela_agent.registry import ActionError

        raise ActionError("Cloudflare: Record does not exist.")

    def delete(self, zone_id, record_id):
        self.data[zone_id] = [r for r in self.data[zone_id] if r["id"] != record_id]


class FakePowerDNS:
    def __init__(self, base) -> None:
        self.base = base
        self.available = True
        self.fail_reload = False
        self.files: dict[str, str] = {}
        self.reloads: list[str | None] = []

    def write(self, zones: dict[str, str]) -> None:
        self.files = dict(zones)

    def reload(self, zone=None) -> None:
        from tela_agent.registry import ActionError

        if self.fail_reload:
            raise ActionError("PowerDNS: Failed to parse zone file")
        self.reloads.append(zone)
