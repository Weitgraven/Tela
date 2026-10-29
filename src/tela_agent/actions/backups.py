"""Backup destinations, running backups, and restoring websites."""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import threading
import time
from typing import Any

from .. import backups, settings, validate
from ..registry import ActionError, action
from ..restic import TYPES, RepoMissing, WrongPassword

_lock = threading.RLock()

_NAME = re.compile(r"^[^\x00-\x1f]{1,60}$")
_LOCAL_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_ENDPOINT = re.compile(r"^[a-z0-9.-]+(:[0-9]{1,5})?$")
_S3_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_B2_BUCKET = re.compile(r"^[A-Za-z0-9-]{6,50}$")
_PREFIX = re.compile(r"^[A-Za-z0-9._/-]{0,200}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9+/=._-]{1,128}$")
_SECRET = re.compile(r"^[\x21-\x7e]{1,200}$")
_REGION = re.compile(r"^[a-z0-9-]{0,30}$")
_SFTP_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_SFTP_PATH = re.compile(r"^[A-Za-z0-9._/~-]{1,200}$")
_TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_SNAPSHOT = re.compile(r"^[0-9a-f]{8,64}$")
_SUBPATH = re.compile(r"^[^\x00-\x1f]{1,500}$")
PASSWORD_ALPHABET = string.ascii_letters + string.digits

LOCATION_FIELDS = {
    "local": ("path",),
    "s3": ("endpoint", "bucket", "prefix", "region"),
    "b2": ("bucket", "prefix"),
    "sftp": ("host", "port", "user", "path"),
}
CREDENTIAL_FIELDS = {"local": (), "s3": ("access_key", "secret_key"),
                     "b2": ("account_id", "account_key"), "sftp": ()}  # fmt: skip


def launch(paths: settings.Paths, job_id: str) -> None:
    """Run a job in its own transient systemd unit (replaced in tests)."""
    env = [f"--setenv={k}={v}" for k, v in os.environ.items()
           if k.startswith("TELA_") or k == "PYTHONPATH"]  # fmt: skip
    cmd = ["systemd-run", "--unit", f"tela-job-{job_id}", "--collect", "--quiet",
           "--nice=10", "--property=IOSchedulingClass=idle", *env,
           sys.executable, "-m", "tela_agent.backuprun", "job", job_id]  # fmt: skip
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise ActionError(f"could not start the job: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _str(params: dict[str, Any], key: str, pattern: re.Pattern[str], what: str,
         required: bool = True) -> str:  # fmt: skip
    value = params.get(key)
    value = "" if value is None else value
    if not isinstance(value, str):
        raise ActionError(f"{what} must be text")
    value = value.strip()
    if not value and not required:
        return ""
    if not pattern.match(value):
        raise ActionError(f"invalid {what}")
    return value


def _host(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionError("enter the SFTP server's host name or IP address")
    value = value.strip()
    with_ip = value.strip("[]")
    try:
        return str(ipaddress.ip_address(with_ip))
    except ValueError:
        return validate.domain(value)


def _int(value: object, low: int, high: int, what: str, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ActionError(f"{what} must be a number from {low} to {high}")
    return value


def _location(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    if kind == "local":
        path = _str(params, "path", _LOCAL_PATH, "folder (use a full path like /backup/tela)")
        if ".." in path.split("/") or path.rstrip("/") in ("", "/etc", "/home", "/var", "/root"):
            raise ActionError("choose a dedicated folder, like /backup/tela")
        return {"path": path.rstrip("/")}
    if kind == "s3":
        endpoint = params.get("endpoint") or ""
        if isinstance(endpoint, str):
            endpoint = endpoint.strip().removeprefix("https://").rstrip("/")
        return {
            "endpoint": _str({"e": endpoint}, "e", _ENDPOINT, "endpoint", required=False)
            or "s3.amazonaws.com",
            "bucket": _str(params, "bucket", _S3_BUCKET, "bucket name"),
            "prefix": _str(params, "prefix", _PREFIX, "folder", required=False).strip("/"),
            "region": _str(params, "region", _REGION, "region", required=False),
        }
    if kind == "b2":
        return {
            "bucket": _str(params, "bucket", _B2_BUCKET, "bucket name"),
            "prefix": _str(params, "prefix", _PREFIX, "folder", required=False).strip("/"),
        }
    if kind == "sftp":
        return {
            "host": _host(params.get("host")),
            "port": _int(params.get("port"), 1, 65535, "port", 22),
            "user": _str(params, "user", _SFTP_USER, "user name"),
            "path": _str(params, "path", _SFTP_PATH, "folder"),
        }
    raise ActionError("choose where to store the backups")


def _credentials(kind: str, params: dict[str, Any], current: dict[str, Any] | None) -> dict:
    result = {}
    if kind == "s3":
        result["access_key"] = _str(params, "access_key", _KEY_ID, "access key")
        secret = params.get("secret_key") or (current or {}).get("secret_key")
        result["secret_key"] = _str({"s": secret}, "s", _SECRET, "secret key")
    elif kind == "b2":
        result["account_id"] = _str(params, "account_id", _KEY_ID, "key ID")
        secret = params.get("account_key") or (current or {}).get("account_key")
        result["account_key"] = _str({"s": secret}, "s", _SECRET, "application key")
    return result


def _options(params: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    current = current or {}
    out: dict[str, Any] = {}
    if "name" in params or not current:
        out["name"] = _str(params, "name", _NAME, "name")
    if "schedule" in params or not current:
        s = params.get("schedule") or {}
        if not isinstance(s, dict):
            raise ActionError("invalid schedule")
        frequency = s.get("frequency", "daily")
        if frequency not in ("daily", "weekly", "manual"):
            raise ActionError("invalid schedule")
        out["schedule"] = {
            "frequency": frequency,
            "time": _str({"t": s.get("time") or "03:00"}, "t", _TIME, "time (use HH:MM)"),
            "weekday": _int(s.get("weekday"), 0, 6, "weekday", 6),
        }
    if "keep" in params or not current:
        k = params.get("keep") or {}
        if not isinstance(k, dict):
            raise ActionError("invalid retention")
        out["keep"] = {
            "daily": _int(k.get("daily"), 0, 366, "daily backups to keep", 7),
            "weekly": _int(k.get("weekly"), 0, 260, "weekly backups to keep", 4),
            "monthly": _int(k.get("monthly"), 0, 240, "monthly backups to keep", 6),
        }
    if "include" in params or not current:
        inc = params.get("include") or {}
        if not isinstance(inc, dict):
            raise ActionError("invalid selection")
        out["include"] = {key: validate.boolean(inc.get(key), default=True)
                          for key in ("files", "databases", "mail", "server")}  # fmt: skip
        if not any(out["include"].values()):
            raise ActionError("choose at least one thing to back up")
    if "notify_email" in params or not current:
        out["notify_email"] = validate.email(params.get("notify_email") or None) or ""
    return out


def public(dest: dict[str, Any]) -> dict[str, Any]:
    view = {k: v for k, v in dest.items() if k not in backups.SECRET_FIELDS}
    for key in ("secret_key", "account_key"):
        if dest.get(key):
            view[key + "_hint"] = "••••" + dest[key][-4:]
    view["location"] = backups.repo(settings.load(), dest).location
    view["next_run"] = backups.next_run(dest)
    return view


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


@action("backup.status")
def status(_params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    key = backups.ssh_key_path(paths).with_suffix(".pub")
    running = [j for j in backups.list_jobs(paths, 20) if j["status"] in ("queued", "running")]
    return {
        "restic_installed": shutil.which("restic") is not None,
        "ssh_public_key": key.read_text().strip() if key.exists() else "",
        "destinations": [public(d) for d in backups.load_destinations(paths)],
        "running": [j["id"] for j in running],
    }


@action("backup.ssh_key")
def ssh_key(_params: dict[str, Any]) -> dict[str, Any]:
    return {"public_key": backups.ensure_ssh_key(settings.load())}


@action("backup.destination.create")
def create_destination(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    kind = params.get("type")
    if kind not in TYPES:
        raise ActionError("choose where to store the backups")
    dest: dict[str, Any] = {
        "id": secrets.token_hex(4),
        "type": kind,
        **_location(kind, params),
        **_credentials(kind, params, None),
        **_options(params),
        "created_at": int(time.time()),
        "last": None,
    }
    existing_password = params.get("repo_password")
    if existing_password:
        dest["repo_password"] = _str(params, "repo_password", _SECRET, "repository password")
    else:
        dest["repo_password"] = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(32))
    if kind == "sftp":
        backups.ensure_ssh_key(paths)
    if kind == "local":
        os.makedirs(dest["path"], mode=0o700, exist_ok=True)

    repo = backups.repo(paths, dest)
    with _lock:
        for other in backups.load_destinations(paths):
            if backups.repo(paths, other).location == repo.location:
                raise ActionError(f"{other['name']} already uses this location")
        connected = False
        try:
            if repo.exists():
                if not existing_password:
                    raise ActionError(
                        "there's already a backup repository here. To use it, enter its "
                        "password; otherwise choose another folder"
                    )
                repo.snapshots()  # checks the password
                connected = True
            else:
                repo.init()
        except WrongPassword as exc:
            raise ActionError("that password doesn't open the repository at this location") from exc
        except RepoMissing as exc:
            raise ActionError(str(exc)) from exc
        backups.save_destination(paths, dest)
    result = public(dest)
    result["connected_existing"] = connected
    if not existing_password:
        result["repo_password"] = dest["repo_password"]
    return result


@action("backup.destination.update")
def update_destination(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    with _lock:
        dest = backups.load_destination(paths, params.get("id"))
        changes = _options(params, dest)
        if any(params.get(f) for f in CREDENTIAL_FIELDS[dest["type"]]):
            changes.update(_credentials(dest["type"], {**dest, **params}, dest))
        candidate = {**dest, **changes}
        if changes.keys() & set(CREDENTIAL_FIELDS[dest["type"]]):
            backups.repo(paths, candidate).snapshots()  # new credentials must work
        backups.save_destination(paths, candidate)
    return public(candidate)


@action("backup.destination.delete")
def delete_destination(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    with _lock:
        dest = backups.load_destination(paths, params.get("id"))
        backups.dest_file(paths, dest["id"]).unlink()
    return {"id": dest["id"], "name": dest["name"]}


@action("backup.destination.password")
def destination_password(params: dict[str, Any]) -> dict[str, Any]:
    dest = backups.load_destination(settings.load(), params.get("id"))
    return {"id": dest["id"], "repo_password": dest["repo_password"],
            "location": backups.repo(settings.load(), dest).location}  # fmt: skip


@action("backup.destination.usage")
def destination_usage(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    dest = backups.load_destination(paths, params.get("id"))
    stats = backups.repo(paths, dest).stats()
    return {"id": dest["id"], "size": stats.get("total_size", 0)}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _domains(value: object) -> list[str] | None:
    if value in (None, []):
        return None
    if not isinstance(value, list) or len(value) > 500:
        raise ActionError("invalid list of websites")
    return [validate.domain(v) for v in value]


@action("backup.run")
def run(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    dest = backups.load_destination(paths, params.get("destination"))
    domains = _domains(params.get("domains"))
    if domains:
        from . import websites

        known = {s.domain for s in websites.load_sites(paths)}
        missing = [d for d in domains if d not in known]
        if missing:
            raise ActionError(f"no website {missing[0]}")
    for job in backups.list_jobs(paths, 20):
        if job.get("destination") == dest["id"] and job["status"] in ("queued", "running"):
            raise ActionError(f"a job on {dest['name']} is already running")
    job = backups.new_job(paths, "backup", destination=dest["id"], domains=domains,
                          trigger="manual")  # fmt: skip
    launch(paths, job["id"])
    return backups.load_job(paths, job["id"])


@action("backup.jobs")
def jobs(params: dict[str, Any]) -> list[dict[str, Any]]:
    paths = settings.load()
    limit = _int(params.get("limit"), 1, 200, "limit", 30)
    return backups.list_jobs(paths, limit)


@action("backup.job.get")
def job(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    data = backups.load_job(paths, params.get("id"))
    data["log"] = backups.job_log(paths, data["id"])
    return data


@action("backup.snapshots")
def snapshots(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    domain = validate.domain(params.get("domain"))
    return {"domain": domain, "snapshots": backups.snapshots_for(paths, domain)}


@action("backup.restore")
def restore(params: dict[str, Any]) -> dict[str, Any]:
    from . import websites

    paths = settings.load()
    domain = validate.domain(params.get("domain"))
    websites._load_site(paths, domain)  # must exist
    dest = backups.load_destination(paths, params.get("destination"))
    snapshot = params.get("snapshot")
    if not isinstance(snapshot, str) or not _SNAPSHOT.match(snapshot):
        raise ActionError("invalid snapshot")
    raw = params.get("parts") or {}
    if not isinstance(raw, dict):
        raise ActionError("invalid selection")
    parts = {k: validate.boolean(raw.get(k)) for k in ("files", "databases", "mail")}
    if not any(parts.values()):
        raise ActionError("choose what to restore")
    sub = params.get("path") or ""
    if sub:
        if not isinstance(sub, str) or not _SUBPATH.match(sub) or ".." in sub.split("/"):
            raise ActionError("invalid path")
        sub = sub.strip().strip("/")
        if sub.split("/")[0] in backups.SITE_EXCLUDES:
            raise ActionError("that folder isn't backed up")
    for j in backups.list_jobs(paths, 20):
        if j["status"] in ("queued", "running") and j.get("destination") == dest["id"]:
            raise ActionError("wait until the running backup or restore has finished")
    job = backups.new_job(paths, "restore", destination=dest["id"], domain=domain,
                          snapshot=snapshot, parts=parts, path=sub)  # fmt: skip
    launch(paths, job["id"])
    return backups.load_job(paths, job["id"])
