"""restic: one encrypted, deduplicated repository per backup destination.

Destinations are local folders, S3-compatible storage, Backblaze B2 or SFTP.
Credentials go to restic through its environment, never on the command line.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .registry import ActionError

TYPES = ("local", "s3", "b2", "sftp")


class ResticError(ActionError):
    pass


class RepoMissing(ResticError):
    """There's no repository at the location yet."""


class WrongPassword(ResticError):
    pass


def _clean(output: str) -> str:
    lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
    # restic ends with a summary like "Fatal: ..."; keep the most useful line.
    for line in reversed(lines):
        if line.startswith("Fatal:"):
            return line.removeprefix("Fatal:").strip()
    return lines[-1] if lines else "restic failed"


class Repo:
    """A restic repository described by a destination dict."""

    def __init__(
        self,
        dest: dict[str, Any],
        ssh_key: Path | None = None,
        known_hosts: Path | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.dest = dest
        self.ssh_key = ssh_key
        self.known_hosts = known_hosts
        self.cache_dir = cache_dir

    # -- location ----------------------------------------------------------
    @property
    def location(self) -> str:
        d = self.dest
        kind = d["type"]
        prefix = (d.get("prefix") or "").strip("/")
        if kind == "local":
            return d["path"]
        if kind == "s3":
            endpoint = d.get("endpoint") or "s3.amazonaws.com"
            return f"s3:https://{endpoint}/{d['bucket']}" + (f"/{prefix}" if prefix else "")
        if kind == "b2":
            return f"b2:{d['bucket']}:{prefix or 'tela'}"
        if kind == "sftp":
            return f"sftp:{d['user']}@{d['host']}:{d['path']}"
        raise ResticError(f"unknown destination type {kind}")

    def _env(self) -> dict[str, str]:
        d = self.dest
        env = {
            "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
            "RESTIC_PASSWORD": d["repo_password"],
            "RESTIC_REPOSITORY": self.location,
            "RESTIC_PROGRESS_FPS": "0.016",
        }
        if self.cache_dir:
            env["RESTIC_CACHE_DIR"] = str(self.cache_dir)
        if d["type"] == "s3":
            env["AWS_ACCESS_KEY_ID"] = d["access_key"]
            env["AWS_SECRET_ACCESS_KEY"] = d["secret_key"]
            if d.get("region"):
                env["AWS_DEFAULT_REGION"] = d["region"]
        elif d["type"] == "b2":
            env["B2_ACCOUNT_ID"] = d["account_id"]
            env["B2_ACCOUNT_KEY"] = d["account_key"]
        return env

    def _options(self) -> list[str]:
        d = self.dest
        if d["type"] != "sftp":
            return []
        ssh = ["ssh", "-p", str(d.get("port") or 22), "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20"]  # fmt: skip
        if self.ssh_key:
            ssh += ["-i", str(self.ssh_key), "-o", "IdentitiesOnly=yes"]
        if self.known_hosts:
            ssh += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        ssh += [f"{d['user']}@{d['host']}", "-s", "sftp"]
        # Every part is validated to contain no spaces or quotes, so restic's
        # own splitting of this string is unambiguous.
        return ["-o", "sftp.command=" + " ".join(ssh)]

    # -- running -----------------------------------------------------------
    def run(self, args: list[str], timeout: int = 3600, log=None) -> str:
        cmd = ["restic", *self._options(), *args]
        try:
            result = subprocess.run(cmd, env=self._env(), capture_output=True, text=True,
                                    timeout=timeout, check=False)  # fmt: skip
        except FileNotFoundError as exc:
            raise ResticError("restic is not installed; run the Tela installer again") from exc
        except subprocess.TimeoutExpired as exc:
            raise ResticError(f"restic took longer than {timeout // 60} minutes") from exc
        if log is not None and result.stderr.strip():
            for line in result.stderr.strip().splitlines()[-20:]:
                log(line)
        if result.returncode != 0:
            output = result.stderr + "\n" + result.stdout
            low = output.lower()
            if result.returncode == 10 or "is there a repository at the following location" in low \
                    or "repository does not exist" in low:  # fmt: skip
                raise RepoMissing("there is no backup repository at this location yet")
            if result.returncode == 12 or "wrong password" in low:
                raise WrongPassword("the repository password is wrong")
            if result.returncode == 3 and args and args[0] == "backup":
                # Some files couldn't be read; the snapshot was still made.
                return result.stdout
            raise ResticError(_clean(output))
        return result.stdout

    def json(self, args: list[str], timeout: int = 600) -> Any:
        out = self.run([*args, "--json"], timeout=timeout)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines:
            return None
        try:
            return json.loads(lines[-1])
        except ValueError as exc:
            raise ResticError("unexpected output from restic") from exc

    # -- operations --------------------------------------------------------
    def exists(self) -> bool:
        try:
            self.run(["cat", "config"], timeout=120)
        except RepoMissing:
            return False
        except WrongPassword:
            return True  # there is one, just not with this password
        return True

    def init(self) -> None:
        self.run(["init"], timeout=300)

    def backup(self, paths: list[Path], tags: list[str], excludes: list[str] = (),
               log=None) -> dict[str, Any]:  # fmt: skip
        args = ["backup", "--json", "--one-file-system", "--host", "tela"]
        for tag in tags:
            args += ["--tag", tag]
        for pattern in excludes:
            args += ["--exclude", pattern]
        args += [str(p) for p in paths]
        out = self.run(args, timeout=6 * 3600, log=log)
        summary: dict[str, Any] = {}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{") and '"message_type":"summary"' in line.replace(" ", ""):
                summary = json.loads(line)
        return summary

    def snapshots(self, tags: list[str] | None = None) -> list[dict[str, Any]]:
        args = ["snapshots"]
        if tags:
            args += ["--tag", ",".join(tags)]
        return self.json(args, timeout=300) or []

    def forget(self, tags: list[str], keep: dict[str, int], prune: bool = True, log=None) -> None:
        policy = []
        for unit in ("daily", "weekly", "monthly"):
            if keep.get(unit):
                policy += [f"--keep-{unit}", str(int(keep[unit]))]
        if not policy:
            return  # no retention set: keep everything
        args = ["forget", "--tag", ",".join(tags), "--group-by", "host,tags", *policy,
                "--keep-last", "1"]  # fmt: skip
        if prune:
            args.append("--prune")
        self.run(args, timeout=6 * 3600, log=log)

    def restore(self, snapshot: str, target: Path, includes: list[str] = (), log=None) -> None:
        if not re.fullmatch(r"[0-9a-f]{8,64}", snapshot):
            raise ResticError("invalid snapshot id")
        args = ["restore", snapshot, "--target", str(target)]
        for pattern in includes:
            args += ["--include", pattern]
        self.run(args, timeout=6 * 3600, log=log)

    def stats(self) -> dict[str, Any]:
        return self.json(["stats", "--mode", "raw-data"], timeout=600) or {}

    def unlock(self) -> None:
        self.run(["unlock"], timeout=120)
