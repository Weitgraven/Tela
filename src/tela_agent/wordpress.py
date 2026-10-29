"""WordPress through WP-CLI, run as the website's own user with its own PHP."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from . import settings
from .ols import Site
from .registry import ActionError

_VERSION = re.compile(r"^\$wp_version\s*=\s*'([^']+)'", re.M)


def user_command(user: str) -> list[str]:
    """Prefix that runs a command as the site's user (replaced in tests)."""
    if os.geteuid() == 0:
        return ["/usr/sbin/runuser", "-u", user, "--"]
    return []


def php_binary(paths: settings.Paths, site: Site) -> Path:
    override = os.environ.get("TELA_WP_PHP")
    if override:
        return Path(override)
    php = paths.ols_root / f"lsphp{site.php_version.replace('.', '')}" / "bin" / "php"
    if not php.exists():
        raise ActionError(f"PHP {site.php_version} has no command-line PHP ({php})")
    return php


def php_string(value: str) -> str:
    """A PHP single-quoted string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def set_define(config: Path, name: str, value: str) -> None:
    """Change define('NAME', '...') in wp-config.php without putting the value on a
    command line (where other users could see it)."""
    text = config.read_text()
    pattern = re.compile(
        r"""define\(\s*['"]"""
        + re.escape(name)
        + r"""['"]\s*,\s*(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")\s*\)"""
    )
    replacement = f"define( '{name}', {php_string(value)} )"
    new, count = pattern.subn(lambda _m: replacement, text, count=1)
    if count == 0:
        raise ActionError(f"{name} isn't set in wp-config.php")
    config.write_text(new)


def get_define(config: Path, name: str) -> str | None:
    match = re.search(
        r"""define\(\s*['"]""" + re.escape(name) + r"""['"]\s*,\s*'((?:[^'\\]|\\.)*)'\s*\)""",
        config.read_text(),
    )
    if not match:
        return None
    return re.sub(r"\\(.)", r"\1", match.group(1))


class WP:
    def __init__(self, paths: settings.Paths, site: Site) -> None:
        self.paths = paths
        self.site = site

    @property
    def docroot(self) -> Path:
        return self.site.home / "public_html"

    @property
    def config(self) -> Path:
        return self.docroot / "wp-config.php"

    def installed(self) -> bool:
        return self.config.exists() and (self.docroot / "wp-includes" / "version.php").exists()

    def version(self) -> str | None:
        try:
            text = (self.docroot / "wp-includes" / "version.php").read_text(errors="replace")
        except OSError:
            return None
        match = _VERSION.search(text)
        return match.group(1) if match else None

    def run(self, *args: str, stdin: str | None = None, timeout: int = 600) -> str:
        if not self.paths.wp_cli.exists():
            raise ActionError("WP-CLI is not installed; run the Tela installer again")
        prefix = user_command(self.site.user)
        cmd = [
            *prefix,
            str(php_binary(self.paths, self.site)),
            "-d", "memory_limit=512M", "-d", "display_errors=stderr",
            str(self.paths.wp_cli), f"--path={self.docroot}", "--no-color", *args,
        ]  # fmt: skip
        if not prefix and os.geteuid() == 0:
            cmd.append("--allow-root")  # only in tests: in production it runs as the site user
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": str(self.site.home),
            "WP_CLI_CACHE_DIR": str(self.site.home / ".wp-cli" / "cache"),
            "WP_CLI_DISABLE_AUTO_CHECK_UPDATE": "1",
        }
        try:
            # The web server (not the site user) has to read static files and
            # walk directories, so new files must not inherit a strict umask
            # (backup jobs run with 077).
            result = subprocess.run(
                cmd,
                umask=0o022,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=self.site.home,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ActionError(f"wp {args[0]} took too long") from exc
        except OSError as exc:
            raise ActionError(f"could not run WP-CLI: {exc}") from exc
        if result.returncode != 0:
            raise ActionError(_error(result.stderr or result.stdout, args[0]))
        return result.stdout

    def json(self, *args: str, timeout: int = 600) -> Any:
        out = self.run(*args, "--format=json", timeout=timeout).strip()
        if not out:
            return []
        try:
            return json.loads(out.splitlines()[-1])
        except ValueError as exc:
            raise ActionError(f"unexpected output from wp {args[0]}") from exc

    def option(self, name: str) -> str:
        return self.run("option", "get", name).strip()


def _error(output: str, command: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    for line in lines:
        if line.startswith("Error:"):
            return line.removeprefix("Error:").strip()
    return f"wp {command} failed: " + (lines[-1] if lines else "no output")
