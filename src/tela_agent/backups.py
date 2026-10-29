"""Backups: what goes into a snapshot, when backups run, and how restores work.

Every website gets its own restic snapshot per run, tagged ``site:<domain>``,
holding its files, a dump of each of its databases and, if the domain has
email, its mail. A separate ``server`` snapshot holds Tela's own configuration
(sites, mail and DNS state, certificates, the panel database) for rebuilding a
server by hand.

Backups and restores run as jobs outside the agent (systemd-run), so a long
backup never blocks the panel. A job's state lives in <backup_dir>/jobs/.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import settings, sysops
from .ols import Site
from .registry import ActionError
from .restic import Repo, ResticError

JOB_ID = re.compile(r"^[0-9]{10}-[0-9a-f]{6}$")
DEST_ID = re.compile(r"^[a-z0-9]{8}$")
SECRET_FIELDS = ("secret_key", "account_key", "repo_password")
SITE_EXCLUDES = ("logs", ".acme", ".tela")


# ---------------------------------------------------------------------------
# System access (replaced in tests)
# ---------------------------------------------------------------------------


class Host:
    """Commands a backup or restore needs besides restic."""

    @staticmethod
    def _run(args: list[str], what: str, timeout: int = 6 * 3600, **kw: Any) -> None:
        kw.setdefault("stdout", subprocess.PIPE)
        try:
            result = subprocess.run(args, stderr=subprocess.PIPE, text=True, timeout=timeout,
                                    check=False, **kw)  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ActionError(f"{what} failed: {exc}") from exc
        if result.returncode != 0:
            raise ActionError(f"{what} failed: {(result.stderr or '').strip()[-300:]}")

    def dump_database(self, name: str, target: Path) -> None:
        with target.open("w") as out:
            self._run(["mariadb-dump", "--single-transaction", "--quick", "--routines",
                       "--triggers", "--events", "--hex-blob", "--default-character-set=utf8mb4",
                       name], f"dump of database {name}", stdout=out)  # fmt: skip

    def import_database(self, name: str, source: Path) -> None:
        with source.open() as sql:
            self._run(["mariadb", "--default-character-set=utf8mb4", name],
                      f"import of database {name}", stdin=sql)  # fmt: skip

    def sync(
        self, source: Path, target: Path, delete: bool, excludes: tuple[str, ...] = ()
    ) -> None:
        args = ["rsync", "-a", "--numeric-ids"]
        if delete:
            args.append("--delete")
        for pattern in excludes:
            args += ["--exclude", pattern]
        src = f"{source}/" if source.is_dir() else str(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._run([*args, src, str(target)], "copying files back")

    def chown_tree(self, path: Path, user: str, group: str) -> None:
        self._run(["chown", "-R", "--no-dereference", f"{user}:{group}", str(path)], "chown")

    def send_mail(self, to: str, subject: str, body: str) -> None:
        message = (
            f"To: {to}\nSubject: {subject}\nContent-Type: text/plain; charset=utf-8\n\n{body}\n"
        )
        with contextlib.suppress(Exception):
            subprocess.run(["/usr/sbin/sendmail", "-t"], input=message, text=True, timeout=60,
                           capture_output=True, check=False)  # fmt: skip


def host() -> Host:
    return Host()


def mariadb_server(paths: settings.Paths):
    """Database server (replaced in tests)."""
    from .mariadb import MariaDB

    return MariaDB(paths.mysql_socket)


def mail_system():
    from .actions import mail

    return mail.system()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sysops.write_atomic(path, json.dumps(data, indent=2) + "\n", 0o600)


def dest_file(paths: settings.Paths, dest_id: str) -> Path:
    if not DEST_ID.match(dest_id or ""):
        raise ActionError("invalid destination")
    return paths.backup_dir / "destinations" / f"{dest_id}.json"


def load_destinations(paths: settings.Paths) -> list[dict[str, Any]]:
    folder = paths.backup_dir / "destinations"
    if not folder.exists():
        return []
    dests = [json.loads(f.read_text()) for f in sorted(folder.glob("*.json"))]
    return sorted(dests, key=lambda d: d.get("created_at", 0))


def load_destination(paths: settings.Paths, dest_id: object) -> dict[str, Any]:
    if not isinstance(dest_id, str):
        raise ActionError("invalid destination")
    try:
        return json.loads(dest_file(paths, dest_id).read_text())
    except OSError as exc:
        raise ActionError("no such backup destination") from exc


def save_destination(paths: settings.Paths, dest: dict[str, Any]) -> None:
    _save(dest_file(paths, dest["id"]), dest)


def update_destination(paths: settings.Paths, dest_id: str, **changes: Any) -> None:
    """Change a few fields, re-reading the file first (a job may run meanwhile)."""
    try:
        dest = load_destination(paths, dest_id)
    except ActionError:
        return  # deleted while the job ran
    dest.update(changes)
    save_destination(paths, dest)


def job_file(paths: settings.Paths, job_id: str) -> Path:
    if not JOB_ID.match(job_id or ""):
        raise ActionError("invalid job")
    return paths.backup_dir / "jobs" / f"{job_id}.json"


def new_job(paths: settings.Paths, kind: str, **params: Any) -> dict[str, Any]:
    job = {
        "id": f"{int(time.time())}-{secrets.token_hex(3)}",
        "kind": kind,
        "status": "queued",
        "created_at": int(time.time()),
        "started_at": None,
        "finished_at": None,
        "message": "",
        "results": [],
        **params,
    }
    _save(job_file(paths, job["id"]), job)
    _prune_jobs(paths)
    return job


def load_job(paths: settings.Paths, job_id: object) -> dict[str, Any]:
    if not isinstance(job_id, str):
        raise ActionError("invalid job")
    try:
        return json.loads(job_file(paths, job_id).read_text())
    except OSError as exc:
        raise ActionError("no such job") from exc


def save_job(paths: settings.Paths, job: dict[str, Any]) -> None:
    _save(job_file(paths, job["id"]), job)


def list_jobs(paths: settings.Paths, limit: int = 50) -> list[dict[str, Any]]:
    folder = paths.backup_dir / "jobs"
    if not folder.exists():
        return []
    files = sorted(folder.glob("*.json"), reverse=True)[:limit]
    jobs = []
    for f in files:
        with contextlib.suppress(OSError, ValueError):
            jobs.append(json.loads(f.read_text()))
    return jobs


def _prune_jobs(paths: settings.Paths, keep: int = 200) -> None:
    folder = paths.backup_dir / "jobs"
    for old in sorted(folder.glob("*.json"), reverse=True)[keep:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".log").unlink(missing_ok=True)


def job_log(paths: settings.Paths, job_id: str, tail: int = 200) -> str:
    path = job_file(paths, job_id).with_suffix(".log")
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-tail:])


class Log:
    def __init__(self, paths: settings.Paths, job_id: str) -> None:
        self.path = job_file(paths, job_id).with_suffix(".log")

    def __call__(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.path.open("a") as fh:
            fh.write(f"{stamp} {line}\n")
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)


# ---------------------------------------------------------------------------
# SSH key for SFTP destinations
# ---------------------------------------------------------------------------


def ssh_key_path(paths: settings.Paths) -> Path:
    return paths.backup_dir / "ssh" / "id_ed25519"


def known_hosts(paths: settings.Paths) -> Path:
    return paths.backup_dir / "ssh" / "known_hosts"


def ensure_ssh_key(paths: settings.Paths) -> str:
    key = ssh_key_path(paths)
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "tela-backup", "-f", str(key)],
            capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
        if result.returncode != 0:
            raise ActionError(f"could not create an SSH key: {result.stderr.strip()}")
    return key.with_suffix(".pub").read_text().strip()


def repo(paths: settings.Paths, dest: dict[str, Any]) -> Repo:
    return Repo(dest, ssh_key=ssh_key_path(paths), known_hosts=known_hosts(paths),
                cache_dir=paths.backup_staging / "cache")  # fmt: skip


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def last_slot(schedule: dict[str, Any], now: datetime) -> datetime | None:
    """The most recent moment a scheduled backup should have started."""
    frequency = schedule.get("frequency", "daily")
    if frequency == "manual":
        return None
    hour, minute = (int(x) for x in schedule.get("time", "03:00").split(":"))
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == "daily":
        return slot if slot <= now else slot - timedelta(days=1)
    weekday = int(schedule.get("weekday", 0))  # Monday = 0
    slot -= timedelta(days=(slot.weekday() - weekday) % 7)
    return slot if slot <= now else slot - timedelta(days=7)


def is_due(dest: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now()
    slot = last_slot(dest.get("schedule", {}), now)
    if slot is None:
        return False
    started = (dest.get("last") or {}).get("scheduled_at") or dest.get("created_at", 0)
    return slot.timestamp() > started


def next_run(dest: dict[str, Any], now: datetime | None = None) -> int | None:
    now = now or datetime.now()
    schedule = dest.get("schedule", {})
    if schedule.get("frequency", "daily") == "manual":
        return None
    step = timedelta(days=1 if schedule.get("frequency") == "daily" else 7)
    slot = last_slot(schedule, now)
    assert slot is not None
    return int((slot + step).timestamp())


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def _sites(paths: settings.Paths) -> list[Site]:
    from .actions import websites

    return websites.load_sites(paths)


@contextlib.contextmanager
def _lock(paths: settings.Paths, dest_id: str):
    folder = paths.backup_dir / "locks"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / f"{dest_id}.lock").open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActionError("another backup or restore on this destination is running") from exc
        yield


def _site_staging(paths: settings.Paths, domain: str) -> Path:
    return paths.backup_staging / "sites" / domain


def _stage_site(paths: settings.Paths, site: Site, dest: dict[str, Any], log) -> list[Path]:
    """Prepare everything of one site for restic; returns the paths to back up."""
    include = dest.get("include", {})
    staging = _site_staging(paths, site.domain)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, mode=0o700)
    targets: list[Path] = []
    if include.get("files", True) and site.home.exists():
        targets.append(site.home)
    if include.get("databases", True) and site.databases:
        (staging / "databases").mkdir(mode=0o700)
        for name in site.databases:
            log(f"{site.domain}: dumping database {name}")
            host().dump_database(name, staging / "databases" / f"{name}.sql")
    if include.get("mail", True):
        mail_state = paths.mail_dir / "domains" / f"{site.domain}.json"
        if mail_state.exists():
            mail = mail_system()
            (staging / "mail").mkdir(mode=0o700)
            shutil.copy2(mail_state, staging / "mail" / "domain.json")
            dkim = mail.dkim_key_path(site.domain, "tela")
            if dkim.exists():
                shutil.copy2(dkim, staging / "mail" / "dkim.key")
            vmail = mail.vmail_root / site.domain
            if vmail.exists():
                targets.append(vmail)
    state = paths.state_dir / f"{site.domain}.json"
    if state.exists():
        shutil.copy2(state, staging / "website.json")
    targets.insert(0, staging)
    return targets


def _stage_server(paths: settings.Paths) -> list[Path]:
    staging = paths.backup_staging / "server"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, mode=0o700)
    if paths.panel_db.exists():
        try:
            source = sqlite3.connect(f"file:{paths.panel_db}?mode=ro", uri=True)
            target = sqlite3.connect(staging / "panel.sqlite")
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
        except (sqlite3.Error, OSError) as exc:
            raise ActionError(f"could not copy the panel database: {exc}") from exc
    mail = mail_system()
    candidates = [
        staging,
        paths.state_dir.parent,  # /etc/tela
        paths.letsencrypt,
        paths.ols_root / "conf",
        mail.postfix_dir,
        mail.dovecot_users.parent,
        mail.dkim_dir,
        paths.pdns_dir,
    ]
    return [p for p in candidates if p.exists()]


def _excludes(site: Site) -> list[str]:
    return [str(site.home / name) for name in SITE_EXCLUDES]


def run_backup(paths: settings.Paths, job: dict[str, Any]) -> None:
    dest = load_destination(paths, job["destination"])
    log = Log(paths, job["id"])
    r = repo(paths, dest)
    wanted = job.get("domains")  # None = everything
    with _lock(paths, dest["id"]):
        log(f"Backing up to {dest['name']} ({r.location})")
        if not r.exists():
            raise ActionError("the backup repository is gone; check the destination")
        failures = 0
        for site in _sites(paths):
            if wanted and site.domain not in wanted:
                continue
            started = time.time()
            try:
                targets = _stage_site(paths, site, dest, log)
                log(f"{site.domain}: uploading")
                summary = r.backup(targets, ["tela", f"site:{site.domain}"], _excludes(site), log)
                job["results"].append({
                    "name": site.domain, "ok": True,
                    "snapshot": summary.get("snapshot_id", "")[:8],
                    "added": summary.get("data_added", 0),
                    "size": summary.get("total_bytes_processed", 0),
                    "seconds": round(time.time() - started),
                })  # fmt: skip
                log(f"{site.domain}: done")
            except ActionError as exc:
                failures += 1
                job["results"].append({"name": site.domain, "ok": False, "message": str(exc)})
                log(f"{site.domain}: FAILED: {exc}")
            finally:
                shutil.rmtree(_site_staging(paths, site.domain), ignore_errors=True)
            save_job(paths, job)

        if not wanted and dest.get("include", {}).get("server", True):
            try:
                log("server configuration: uploading")
                summary = r.backup(_stage_server(paths), ["tela", "server"], (), log)
                job["results"].append(
                    {
                        "name": "Server configuration",
                        "ok": True,
                        "snapshot": summary.get("snapshot_id", "")[:8],
                        "added": summary.get("data_added", 0),
                        "size": summary.get("total_bytes_processed", 0),
                    }
                )
            except ActionError as exc:
                failures += 1
                job["results"].append({"name": "Server configuration", "ok": False,
                                       "message": str(exc)})  # fmt: skip
                log(f"server configuration: FAILED: {exc}")
            finally:
                shutil.rmtree(paths.backup_staging / "server", ignore_errors=True)
            save_job(paths, job)

        keep = dest.get("keep", {})
        if not wanted and any(keep.values()):
            try:
                log("Removing old snapshots")
                r.forget(["tela"], keep, prune=True, log=log)
            except ActionError as exc:
                log(f"Cleaning up old snapshots failed: {exc}")
        total = len(job["results"])
        if failures:
            job["status"] = "partial" if failures < total else "failed"
            job["message"] = f"{failures} of {total} failed"
        else:
            job["status"] = "ok"
            job["message"] = f"{total} backed up" if total else "nothing to back up"


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def snapshots_for(paths: settings.Paths, domain: str) -> list[dict[str, Any]]:
    result = []
    errors = []
    for dest in load_destinations(paths):
        try:
            snaps = repo(paths, dest).snapshots(["tela", f"site:{domain}"])
        except ActionError as exc:
            errors.append({"destination": dest["name"], "message": str(exc)})
            continue
        for s in snaps:
            result.append({
                "id": s["short_id"] if "short_id" in s else s["id"][:8],
                "full_id": s["id"],
                "time": s["time"],
                "destination": dest["id"],
                "destination_name": dest["name"],
                "size": (s.get("summary") or {}).get("total_bytes_processed"),
                "has_mail": any("/vmail/" in p or p.endswith(f"/{domain}") and "vmail" in p
                                for p in s.get("paths", [])),
            })  # fmt: skip
    result.sort(key=lambda s: s["time"], reverse=True)
    return result if not errors or result else _raise_all(errors)


def _raise_all(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raise ActionError("; ".join(f"{e['destination']}: {e['message']}" for e in errors))


def _find(snapshot: dict[str, Any], test) -> str | None:
    return next((p for p in snapshot.get("paths", []) if test(p)), None)


def run_restore(paths: settings.Paths, job: dict[str, Any]) -> None:
    from .actions import websites

    dest = load_destination(paths, job["destination"])
    log = Log(paths, job["id"])
    r = repo(paths, dest)
    domain = job["domain"]
    site = websites._load_site(paths, domain)
    parts = job.get("parts", {})
    sub = (job.get("path") or "").strip("/")

    with _lock(paths, dest["id"]):
        snaps = [s for s in r.snapshots(["tela", f"site:{domain}"])
                 if s["id"].startswith(job["snapshot"])]  # fmt: skip
        if len(snaps) != 1:
            raise ActionError("that snapshot doesn't exist (anymore)")
        snap = snaps[0]
        staging_path = _find(snap, lambda p: p.rstrip("/").endswith(f"/sites/{domain}"))
        mail_path = _find(snap, lambda p: p.rstrip("/").endswith(f"/{domain}") and p != staging_path
                          and p.rstrip("/") != str(site.home))  # fmt: skip
        home_path = _find(snap, lambda p: p not in (staging_path, mail_path))

        work = paths.backup_staging / "restore" / job["id"]
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, mode=0o700)
        try:
            includes = []
            if parts.get("files") and home_path:
                includes.append(f"{home_path}/{sub}" if sub else home_path)
            if parts.get("databases") and staging_path:
                includes.append(f"{staging_path}/databases")
            if parts.get("mail") and mail_path:
                includes.append(mail_path)
            if not includes:
                raise ActionError("this snapshot doesn't contain what you chose to restore")
            log(f"Downloading snapshot {snap['short_id'] if 'short_id' in snap else snap['id'][:8]}"
                f" from {dest['name']}")  # fmt: skip
            r.restore(snap["id"], work, includes, log)
            failures = 0

            if parts.get("files") and home_path:
                failures += _step(job, log, "Files", lambda: _restore_files(
                    paths, site, work / home_path.lstrip("/"), sub))  # fmt: skip
            if parts.get("databases") and staging_path:
                dumps = work / staging_path.lstrip("/") / "databases"
                for dump in sorted(dumps.glob("*.sql")) if dumps.exists() else []:
                    name = dump.stem

                    def restore_db(d: Path = dump, n: str = name) -> str:
                        return _restore_database(paths, site, n, d)

                    failures += _step(job, log, f"Database {name}", restore_db)
            if parts.get("mail") and mail_path:
                failures += _step(job, log, "Mail", lambda: _restore_mail(
                    domain, work / mail_path.lstrip("/")))  # fmt: skip
        finally:
            shutil.rmtree(work, ignore_errors=True)
        total = len(job["results"])
        job["status"] = "ok" if not failures else ("partial" if failures < total else "failed")
        job["message"] = "restored" if not failures else f"{failures} of {total} failed"


def _step(job: dict[str, Any], log, name: str, fn) -> int:
    try:
        note = fn()
        job["results"].append({"name": name, "ok": True, "message": note or ""})
        log(f"{name}: restored" + (f" ({note})" if note else ""))
        return 0
    except ActionError as exc:
        job["results"].append({"name": name, "ok": False, "message": str(exc)})
        log(f"{name}: FAILED: {exc}")
        return 1


def _restore_files(paths: settings.Paths, site: Site, source: Path, sub: str) -> str:
    from .actions import websites

    h = host()
    if sub:
        item = source / sub
        if not item.exists() and not item.is_symlink():
            raise ActionError(f"{sub} isn't in this snapshot")
        target = site.home / sub
        if not target.resolve().is_relative_to(site.home.resolve()):
            raise ActionError("invalid path")
        if item.is_dir():
            h.sync(item, target, delete=True)
        else:
            h.sync(item, target, delete=False)
        h.chown_tree(target, site.user, site.user)
        websites._prepare_dirs(paths, site)
        return sub
    if not source.exists():
        raise ActionError("no website files in this snapshot")
    excludes = tuple(f"/{name}" for name in SITE_EXCLUDES)
    h.sync(source, site.home, delete=True, excludes=excludes)
    for entry in site.home.iterdir():
        if entry.name not in SITE_EXCLUDES:
            h.chown_tree(entry, site.user, site.user)
    websites._prepare_dirs(paths, site)
    return ""


def _restore_database(paths: settings.Paths, site: Site, name: str, dump: Path) -> str:
    if name not in site.databases:
        raise ActionError(f"{name} no longer belongs to {site.domain}; create it first")
    server = mariadb_server(paths)
    server.recreate_database(name)
    host().import_database(name, dump)
    return ""


def _restore_mail(domain: str, source: Path) -> str:
    if not source.exists():
        raise ActionError("no mail in this snapshot")
    mail = mail_system()
    target = mail.vmail_root / domain
    host().sync(source, target, delete=False)
    host().chown_tree(target, "vmail", "vmail")
    return "messages deleted since then are back; newer ones were kept"


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------


def execute(paths: settings.Paths, job_id: str) -> dict[str, Any]:
    job = load_job(paths, job_id)
    if job["status"] != "queued":
        return job
    job.update(status="running", started_at=int(time.time()))
    save_job(paths, job)
    log = Log(paths, job_id)
    try:
        runner = JOB_KINDS.get(job["kind"])
        if runner is None:
            raise ActionError(f"unknown job kind {job['kind']}")
        runner(paths, job)
    except (ActionError, ResticError) as exc:
        job.update(status="failed", message=str(exc))
        log(f"FAILED: {exc}")
    except Exception as exc:  # noqa: BLE001 - a job must always end with a status
        job.update(status="failed", message=f"unexpected error: {exc}")
        log(traceback.format_exc())
    job["finished_at"] = int(time.time())
    save_job(paths, job)
    log(f"Finished: {job['status']}")
    if job["kind"] == "backup":
        update_destination(paths, job["destination"], last={
            "job": job["id"], "status": job["status"], "message": job["message"],
            "finished_at": job["finished_at"],
            "scheduled_at": job.get("scheduled_at") or (
                load_destination_safe(paths, job["destination"]).get("last") or {}
            ).get("scheduled_at"),
        })  # fmt: skip
        _notify(paths, job)
    return job


# Other modules (WordPress) add their own kinds of jobs here.
JOB_KINDS = {"backup": run_backup, "restore": run_restore}


def load_destination_safe(paths: settings.Paths, dest_id: str) -> dict[str, Any]:
    try:
        return load_destination(paths, dest_id)
    except ActionError:
        return {}


def _notify(paths: settings.Paths, job: dict[str, Any]) -> None:
    dest = load_destination_safe(paths, job["destination"])
    to = dest.get("notify_email")
    if not to or job["status"] == "ok":
        return
    lines = [f"Backup to {dest.get('name')}: {job['status']} ({job['message']})", ""]
    for r in job["results"]:
        lines.append(f"- {r['name']}: {'ok' if r['ok'] else 'FAILED: ' + r.get('message', '')}")
    lines += ["", job_log(paths, job["id"], tail=30)]
    host().send_mail(to, f"[Tela] Backup {job['status']}: {dest.get('name')}", "\n".join(lines))


def run_scheduled(paths: settings.Paths, now: datetime | None = None) -> list[str]:
    """Start every destination whose scheduled time has passed. Returns job ids."""
    now = now or datetime.now()
    ran = []
    for dest in load_destinations(paths):
        if not is_due(dest, now):
            continue
        slot = last_slot(dest["schedule"], now)
        assert slot is not None
        last = {**(dest.get("last") or {}), "scheduled_at": int(slot.timestamp())}
        update_destination(paths, dest["id"], last=last)
        job = new_job(paths, "backup", destination=dest["id"], domains=None, trigger="schedule",
                      scheduled_at=int(slot.timestamp()))  # fmt: skip
        execute(paths, job["id"])
        ran.append(job["id"])
    return ran
