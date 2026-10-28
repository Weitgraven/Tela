"""Operations on the host system: Linux users, ownership, services, certificates.

Everything that touches the real system goes through `System`, so tests can swap
in a fake. Commands are always run as argument lists, never through a shell.
"""

from __future__ import annotations

import contextlib
import grp
import os
import pwd
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .registry import ActionError


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


class System:
    # -- users -------------------------------------------------------------
    def user_exists(self, name: str) -> bool:
        try:
            pwd.getpwnam(name)
        except KeyError:
            return False
        return True

    def create_site_user(self, name: str, home: Path) -> None:
        result = _run(
            ["useradd", "--no-create-home", "--home-dir", str(home),
             "--shell", "/usr/sbin/nologin", "--user-group", name]
        )  # fmt: skip
        if result.returncode != 0:
            raise ActionError(f"could not create user {name}: {result.stderr.strip()}")

    def delete_site_user(self, name: str) -> None:
        if self.user_exists(name):
            _run(["pkill", "-u", name])
            result = _run(["userdel", name])
            if result.returncode not in (0, 6):
                raise ActionError(f"could not delete user {name}: {result.stderr.strip()}")

    # -- files -------------------------------------------------------------
    def chown(self, path: Path, user: str, group: str) -> None:
        shutil.chown(path, user=user, group=group)

    def remove_tree(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    # -- web server --------------------------------------------------------
    def ols_group(self, httpd_config: Path) -> tuple[str, str]:
        """The user and group OpenLiteSpeed's workers run as."""
        user, group = "nobody", "nogroup"
        with contextlib.suppress(OSError):
            for line in httpd_config.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] == "user":
                    user = parts[1]
                elif len(parts) == 2 and parts[0] == "group":
                    group = parts[1]
        return user, group

    def reload_web_server(self, ols_root: Path) -> None:
        """Gracefully restart OpenLiteSpeed and check the new configuration was loaded.

        Goes through systemd rather than running lswsctrl directly: lswsctrl looks
        for the server's PID file in /tmp, which this service can't see
        (PrivateTmp), so it would start a second server that fails to bind.

        A graceful restart replaces the server process, so the PID changes once
        the new configuration is live. If it doesn't change, the new process
        failed to start (the old one keeps serving the old configuration).
        """
        before = _ols_pid()
        result = _run(["systemctl", "reload-or-restart", "lsws"], timeout=90)
        if result.returncode != 0:
            raise ActionError(f"web server restart failed: {result.stderr.strip()}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.5)
            pid = _ols_pid()
            if pid and pid != before and _pid_alive(pid):
                return
        detail = _ols_errors(ols_root)
        raise ActionError(
            "the web server did not load the new configuration"
            + (f": {detail}" if detail else "; see /usr/local/lsws/logs/error.log")
        )

    def php_versions(self, ols_root: Path) -> list[str]:
        versions = []
        for binary in sorted(ols_root.glob("lsphp[0-9][0-9]/bin/lsphp")):
            digits = binary.parent.parent.name.removeprefix("lsphp")
            versions.append(f"{digits[0]}.{digits[1:]}")
        return versions

    # -- certificates ------------------------------------------------------
    def resolve(self, name: str) -> set[str]:
        try:
            return {info[4][0] for info in socket.getaddrinfo(name, None)}
        except OSError:
            return set()

    def http_fetch(self, url: str, host: str | None = None, timeout: float = 10) -> tuple[int, str]:
        """GET a URL the way Let's Encrypt does (following redirects).

        Returns (status, body); status 0 means the server couldn't be reached.
        With `host`, the request is sent to the URL's address with that Host header.
        """
        headers = {"User-Agent": "Tela challenge self-check"}
        if host:
            headers["Host"] = host
        request = urllib.request.Request(url, headers=headers)  # noqa: S310 (http only)
        # Never go through a proxy: the point is to see what the world sees directly.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(4096).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, ""
        except (urllib.error.URLError, OSError, ValueError):
            return 0, ""

    def certbot_issue(
        self, cert_name: str, names: list[str], webroot: Path, email: str | None
    ) -> None:
        args = [
            "certbot", "certonly", "--webroot", "--webroot-path", str(webroot),
            "--cert-name", cert_name, "--non-interactive", "--agree-tos",
            "--keep-until-expiring", "--expand", "--key-type", "ecdsa",
        ]  # fmt: skip
        args += ["--email", email] if email else ["--register-unsafely-without-email"]
        for name in names:
            args += ["-d", name]
        result = _run(args, timeout=300)
        if result.returncode != 0:
            raise ActionError(_certbot_error(result.stdout + "\n" + result.stderr))

    def certbot_delete(self, cert_name: str) -> None:
        _run(["certbot", "delete", "--non-interactive", "--cert-name", cert_name], timeout=60)

    def cert_expiry(self, cert: Path) -> int | None:
        result = _run(["openssl", "x509", "-noout", "-enddate", "-in", str(cert)])
        if result.returncode != 0 or "=" not in result.stdout:
            return None
        text = result.stdout.strip().split("=", 1)[1]
        try:
            when = datetime.strptime(text, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        except ValueError:
            return None
        return int(when.timestamp())


OLS_PID_FILES = (Path("/run/openlitespeed.pid"), Path("/var/run/openlitespeed.pid"))


def _ols_pid() -> int | None:
    for pid_file in OLS_PID_FILES:
        with contextlib.suppress(OSError, ValueError):
            return int(pid_file.read_text().strip())
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ols_errors(ols_root: Path, limit: int = 3) -> str:
    """The last few [ERROR] lines of the web server's error log, without timestamps."""
    log = ols_root / "logs" / "error.log"
    try:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(fh.tell() - 16384, 0))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    errors = [line.split("] ", 2)[-1] for line in lines if "[ERROR]" in line]
    return "; ".join(errors[-limit:])


def _certbot_error(output: str) -> str:
    """Pull the useful part out of certbot's output."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for marker in ("Detail:", "Type:"):
        for line in lines:
            if line.startswith(marker):
                return "certificate request failed: " + line.removeprefix(marker).strip()
    for line in lines:
        if "error" in line.lower() or "problem" in line.lower():
            return "certificate request failed: " + line
    return "certificate request failed; check that the domain points to this server"


def group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
    except KeyError:
        return False
    return True


def write_atomic(path: Path, content: str, mode: int = 0o644) -> None:
    """Replace a file atomically. An existing file keeps its owner and mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        st = path.stat()
    except FileNotFoundError:
        os.chmod(tmp, mode)
    else:
        os.chmod(tmp, st.st_mode & 0o7777)
        if os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)


system = System()
