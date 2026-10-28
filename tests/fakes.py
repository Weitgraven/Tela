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
