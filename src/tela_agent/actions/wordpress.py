"""WordPress: install, updates, staging copies and cloning.

Everything that takes a while runs as a job (see tela_agent.backups), so the
panel never waits on a download or a database copy.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .. import backups, settings, sysops, validate
from ..ols import Site
from ..registry import ActionError, action
from ..wordpress import WP, get_define, set_define
from . import backups as backup_actions
from . import databases, websites

LOCALES = (
    "en_US",
    "nl_NL",
    "de_DE",
    "fr_FR",
    "es_ES",
    "it_IT",
    "pt_PT",
    "pt_BR",
    "da_DK",
    "sv_SE",
    "nb_NO",
    "fi",
    "pl_PL",
    "cs_CZ",
    "en_GB",
)
_TITLE = re.compile(r"^[^\x00-\x1f]{1,100}$")
_ADMIN_USER = re.compile(r"^[A-Za-z0-9._@-]{3,60}$")
_PASSWORD = re.compile(r"^[^\x00-\x1f]{10,128}$")

HTACCESS = """# BEGIN WordPress
<IfModule mod_rewrite.c>
RewriteEngine On
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]
RewriteBase /
RewriteRule ^index\\.php$ - [L]
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule . /index.php [L]
</IfModule>
# END WordPress
"""


# ---------------------------------------------------------------------------
# State: which site is a staging copy of which
# ---------------------------------------------------------------------------


def _meta_file(paths: settings.Paths, domain: str) -> Path:
    return paths.wordpress_dir / f"{domain}.json"


def load_meta(paths: settings.Paths, domain: str) -> dict[str, Any]:
    try:
        return json.loads(_meta_file(paths, domain).read_text())
    except (OSError, ValueError):
        return {}


def save_meta(paths: settings.Paths, domain: str, **changes: Any) -> None:
    meta = {**load_meta(paths, domain), **changes}
    meta = {k: v for k, v in meta.items() if v is not None}
    path = _meta_file(paths, domain)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sysops.write_atomic(path, json.dumps(meta, indent=2) + "\n", 0o600)


def _relations(paths: settings.Paths, sites: dict[str, Site]) -> dict[str, dict[str, Any]]:
    """staging/staging_of links, ignoring ones whose other side is gone."""
    result = {}
    for domain in sites:
        meta = load_meta(paths, domain)
        result[domain] = {
            "staging": meta.get("staging") if meta.get("staging") in sites else None,
            "staging_of": meta.get("staging_of") if meta.get("staging_of") in sites else None,
        }
    return result


@contextlib.contextmanager
def _site_lock(paths: settings.Paths, *domains: str):
    folder = paths.wordpress_dir / "locks"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    handles = []
    try:
        for domain in sorted(set(domains)):
            fh = (folder / f"{domain}.lock").open("w")
            handles.append(fh)
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ActionError(f"another WordPress task on {domain} is running") from exc
        yield
    finally:
        for fh in handles:
            fh.close()


def _url(site: Site) -> str:
    return ("https://" if site.ssl_cert else "http://") + site.domain


def _busy(paths: settings.Paths, *domains: str) -> None:
    for job in backups.list_jobs(paths, 30):
        if (
            job["status"] in ("queued", "running")
            and job["kind"].startswith("wp-")
            and (job.get("domain") in domains or job.get("target") in domains)
        ):
            raise ActionError("another WordPress task on this website is running")


def _start(paths: settings.Paths, kind: str, **params: Any) -> dict[str, Any]:
    job = backups.new_job(paths, kind, **params)
    backup_actions.launch(paths, job["id"])
    return backups.load_job(paths, job["id"])


# ---------------------------------------------------------------------------
# Listing and details
# ---------------------------------------------------------------------------


@action("wordpress.list")
def list_sites(_params: dict[str, Any]) -> list[dict[str, Any]]:
    paths = settings.load()
    sites = {s.domain: s for s in websites.load_sites(paths)}
    relations = _relations(paths, sites)
    result = []
    for domain, site in sorted(sites.items()):
        wp = WP(paths, site)
        installed = wp.installed()
        result.append(
            {
                "domain": domain,
                "installed": installed,
                "version": wp.version() if installed else None,
                "url": _url(site),
                **relations[domain],
            }
        )
    return result


@action("wordpress.info")
def info(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    site = websites._load_site(paths, validate.domain(params.get("domain")))
    wp = WP(paths, site)
    if not wp.installed():
        raise ActionError(f"WordPress isn't installed on {site.domain}")
    sites = {s.domain: s for s in websites.load_sites(paths)}
    data: dict[str, Any] = {
        "domain": site.domain,
        "version": wp.version(),
        "url": _url(site),
        **_relations(paths, sites)[site.domain],
    }
    errors = []
    try:
        updates = wp.json("core", "check-update", timeout=90)
        data["core_update"] = max(
            (u["version"] for u in updates),
            default=None,
            key=lambda v: [int(x) for x in re.findall(r"\d+", v)],
        )
    except ActionError as exc:
        data["core_update"] = None
        errors.append(str(exc))
    fields = "name,title,status,version,update,update_version"
    for kind in ("plugin", "theme"):
        try:
            data[kind + "s"] = wp.json(kind, "list", f"--fields={fields}", timeout=120)
        except ActionError as exc:
            data[kind + "s"] = []
            errors.append(str(exc))
    try:
        data["home"] = wp.option("home")
        data["search_engines"] = wp.option("blog_public") == "1"
    except ActionError as exc:
        errors.append(str(exc))
    data["errors"] = errors
    return data


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _str(params: dict[str, Any], key: str, pattern: re.Pattern[str], message: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not pattern.match(
        value.strip() if key != "password" else value
    ):
        raise ActionError(message)
    return value if key == "password" else value.strip()


@action("wordpress.install")
def install(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    site = websites._load_site(paths, validate.domain(params.get("domain")))
    title = _str(params, "title", _TITLE, "enter a site title (up to 100 characters)")
    user = _str(
        params,
        "admin_user",
        _ADMIN_USER,
        "the admin user name may use letters, digits, '.', '_', '@' and '-'",
    )
    email = validate.email(params.get("admin_email"))
    if not email:
        raise ActionError("enter the admin's email address")
    password = _str(params, "password", _PASSWORD, "the admin password needs 10 to 128 characters")
    locale = params.get("locale") or "en_US"
    if locale not in LOCALES:
        raise ActionError("unsupported language")
    wp = WP(paths, site)
    if wp.installed():
        raise ActionError(f"WordPress is already installed on {site.domain}")
    leftovers = (
        [p.name for p in wp.docroot.iterdir() if p.name != "index.html"]
        if wp.docroot.exists()
        else []
    )
    if leftovers:
        raise ActionError(
            f"public_html of {site.domain} isn't empty ({', '.join(sorted(leftovers)[:3])}). "
            "Remove those files first."
        )
    _busy(paths, site.domain)
    # The password goes to WP-CLI on standard input and is never stored: the job
    # keeps only a marker that it still has to be used.
    job = backups.new_job(
        paths,
        "wp-install",
        domain=site.domain,
        title=title,
        admin_user=user,
        admin_email=email,
        locale=locale,
    )
    _secret_file(paths, job["id"]).write_text(password)
    os.chmod(_secret_file(paths, job["id"]), 0o600)
    backup_actions.launch(paths, job["id"])
    return backups.load_job(paths, job["id"])


def _secret_file(paths: settings.Paths, job_id: str) -> Path:
    return backups.job_file(paths, job_id).with_suffix(".secret")


def _free_db_name(paths: settings.Paths, site: Site, wanted: str) -> str:
    db = databases.server(paths)
    for n in range(1, 50):
        short = wanted if n == 1 else f"{wanted}{n}"
        name = databases.full_name(site, short)
        if name not in site.databases and not db.database_exists(name) and not db.user_exists(name):
            return short
    raise ActionError("couldn't find a free database name")


def _create_db(paths: settings.Paths, site: Site, wanted: str = "wp") -> dict[str, Any]:
    return databases.create_database(
        {"domain": site.domain, "name": _free_db_name(paths, site, wanted)}
    )


def run_install(paths: settings.Paths, job: dict[str, Any]) -> None:
    log = backups.Log(paths, job["id"])
    secret = _secret_file(paths, job["id"])
    try:
        password = secret.read_text()
    except OSError as exc:
        raise ActionError("this install was already started") from exc
    finally:
        secret.unlink(missing_ok=True)
    site = websites._load_site(paths, job["domain"])
    wp = WP(paths, site)
    with _site_lock(paths, site.domain):
        default_index = wp.docroot / "index.html"
        if default_index.exists():
            default_index.unlink()
        log("Creating the database")
        db = _create_db(paths, site)
        job["results"].append({"name": "Database", "ok": True, "message": db["name"]})
        try:
            source = os.environ.get("TELA_WP_SOURCE")
            log("Downloading WordPress")
            if source:  # an already downloaded copy (tests, offline installs)
                backups.host().sync(Path(source), wp.docroot, delete=False, excludes=("/.git",))
                backups.host().chown_tree(wp.docroot, site.user, site.user)
            else:
                wp.run("core", "download", f"--locale={job['locale']}", timeout=900)
            log("Writing wp-config.php")
            wp.run(
                "config",
                "create",
                f"--dbname={db['name']}",
                f"--dbuser={db['user']}",
                f"--dbhost=localhost:{paths.mysql_socket}",
                f"--locale={job['locale']}",
                "--skip-check",
                "--prompt=dbpass",
                stdin=db["password"] + "\n",
            )
            wp.run("config", "set", "FS_METHOD", "direct")
            log("Installing")
            wp.run(
                "core",
                "install",
                f"--url={_url(site)}",
                f"--title={job['title']}",
                f"--admin_user={job['admin_user']}",
                f"--admin_email={job['admin_email']}",
                "--skip-email",
                "--prompt=admin_password",
                stdin=password + "\n",
                timeout=300,
            )
            if job["locale"] != "en_US":
                with contextlib.suppress(ActionError):
                    wp.run("language", "core", "install", job["locale"], "--activate", timeout=300)
            wp.run("rewrite", "structure", "/%postname%/")
            htaccess = wp.docroot / ".htaccess"
            if not htaccess.exists():
                htaccess.write_text(HTACCESS)
                backups.host().chown_tree(htaccess, site.user, site.user)
        except ActionError:
            log("Install failed; removing the database again")
            with contextlib.suppress(ActionError):
                databases.delete_database({"domain": site.domain, "name": db["name"]})
            raise
        websites._prepare_dirs(paths, site)
        save_meta(paths, site.domain, installed_by_tela=int(time.time()))
        job["results"].append({"name": "WordPress", "ok": True, "message": wp.version() or ""})
        job["status"] = "ok"
        job["message"] = f"installed; sign in at {_url(site)}/wp-admin/"


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------


@action("wordpress.update")
def update(params: dict[str, Any]) -> dict[str, Any]:
    paths = settings.load()
    site = websites._load_site(paths, validate.domain(params.get("domain")))
    if not WP(paths, site).installed():
        raise ActionError(f"WordPress isn't installed on {site.domain}")
    parts = {
        k: validate.boolean(params.get(k), default=True) for k in ("core", "plugins", "themes")
    }
    if not any(parts.values()):
        raise ActionError("choose what to update")
    _busy(paths, site.domain)
    return _start(paths, "wp-update", domain=site.domain, parts=parts)


def run_update(paths: settings.Paths, job: dict[str, Any]) -> None:
    log = backups.Log(paths, job["id"])
    site = websites._load_site(paths, job["domain"])
    wp = WP(paths, site)
    parts = job["parts"]
    failures = 0
    with _site_lock(paths, site.domain):
        if parts.get("core"):
            before = wp.version()
            try:
                log("Updating WordPress")
                wp.run("core", "update", timeout=900)
                wp.run("core", "update-db", timeout=600)
                after = wp.version()
                note = f"{before} → {after}" if after != before else f"{after}, already up to date"
                job["results"].append({"name": "WordPress", "ok": True, "message": note})
            except ActionError as exc:
                failures += 1
                job["results"].append({"name": "WordPress", "ok": False, "message": str(exc)})
            backups.save_job(paths, job)
        for kind in ("plugin", "theme"):
            if not parts.get(kind + "s"):
                continue
            try:
                log(f"Updating {kind}s")
                updated = wp.json(kind, "update", "--all", timeout=1800) or []
                for u in updated:
                    ok = u.get("status") == "Updated"
                    failures += 0 if ok else 1
                    job["results"].append(
                        {
                            "name": u.get("name", "?"),
                            "ok": ok,
                            "message": f"{u.get('old_version')} → {u.get('new_version')}"
                            if ok
                            else u.get("status", "failed"),
                        }
                    )
                if not updated:
                    job["results"].append(
                        {"name": kind.title() + "s", "ok": True, "message": "already up to date"}
                    )
            except ActionError as exc:
                failures += 1
                job["results"].append(
                    {"name": kind.title() + "s", "ok": False, "message": str(exc)}
                )
            backups.save_job(paths, job)
        with contextlib.suppress(ActionError):
            wp.run("cache", "flush")
    total = len(job["results"])
    job["status"] = "ok" if not failures else ("partial" if failures < total else "failed")
    job["message"] = "up to date" if not failures else f"{failures} of {total} failed"


# ---------------------------------------------------------------------------
# Staging, cloning and pushing a staging copy live
# ---------------------------------------------------------------------------


@action("wordpress.clone")
def clone(params: dict[str, Any]) -> dict[str, Any]:
    """Copy the WordPress site `domain` onto the (empty) website `target`."""
    paths = settings.load()
    source = websites._load_site(paths, validate.domain(params.get("domain")))
    target = websites._load_site(paths, validate.domain(params.get("target")))
    mode = params.get("mode") or "clone"
    if mode not in ("clone", "staging"):
        raise ActionError("invalid mode")
    if source.domain == target.domain:
        raise ActionError("choose another website to copy to")
    if not WP(paths, source).installed():
        raise ActionError(f"WordPress isn't installed on {source.domain}")
    twp = WP(paths, target)
    leftovers = (
        [p.name for p in twp.docroot.iterdir() if p.name != "index.html"]
        if twp.docroot.exists()
        else []
    )
    if leftovers:
        raise ActionError(f"{target.domain} isn't empty; copy only to a new, empty website")
    _busy(paths, source.domain, target.domain)
    return _start(paths, "wp-clone", domain=source.domain, target=target.domain, mode=mode)


@action("wordpress.push")
def push(params: dict[str, Any]) -> dict[str, Any]:
    """Copy a staging site back over its live site."""
    paths = settings.load()
    staging = websites._load_site(paths, validate.domain(params.get("domain")))
    live_domain = load_meta(paths, staging.domain).get("staging_of")
    if not live_domain:
        raise ActionError(f"{staging.domain} isn't a staging copy")
    live = websites._load_site(paths, live_domain)
    parts = {k: validate.boolean(params.get(k), default=True) for k in ("files", "database")}
    if not any(parts.values()):
        raise ActionError("choose what to copy to the live site")
    _busy(paths, staging.domain, live.domain)
    return _start(paths, "wp-push", domain=staging.domain, target=live.domain, parts=parts)


def _copy_files(paths: settings.Paths, src: WP, dst: WP, keep_config: bool) -> None:
    excludes = ("/wp-config.php",) if keep_config else ()
    backups.host().sync(src.docroot, dst.docroot, delete=True, excludes=excludes)
    backups.host().chown_tree(dst.docroot, dst.site.user, dst.site.user)
    websites._prepare_dirs(paths, dst.site)


def _copy_database(paths: settings.Paths, src: WP, db_name: str, work: Path) -> None:
    source_db = get_define(src.config, "DB_NAME")
    if not source_db:
        raise ActionError(f"can't read the database name of {src.site.domain}")
    dump = work / "database.sql"
    backups.host().dump_database(source_db, dump)
    backups.mariadb_server(paths).recreate_database(db_name)
    backups.host().import_database(db_name, dump)


def _replace_urls(dst: WP, old_home: str, old_domain: str, log) -> None:
    new_home = _url(dst.site)
    args = ["--all-tables-with-prefix", "--skip-columns=guid", "--report-changed-only"]
    if old_home.rstrip("/") != new_home:
        log(f"Changing {old_home} to {new_home}")
        dst.run("search-replace", old_home.rstrip("/"), new_home, *args, timeout=1800)
    if old_domain != dst.site.domain:
        dst.run("search-replace", f"//{old_domain}", f"//{dst.site.domain}", *args, timeout=1800)
    dst.run("option", "update", "home", new_home)
    dst.run("option", "update", "siteurl", new_home)


def _work_dir(paths: settings.Paths, job: dict[str, Any]) -> Path:
    work = paths.backup_staging / "wordpress" / job["id"]
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    return work


def run_clone(paths: settings.Paths, job: dict[str, Any]) -> None:
    import shutil

    log = backups.Log(paths, job["id"])
    source = websites._load_site(paths, job["domain"])
    target = websites._load_site(paths, job["target"])
    src, dst = WP(paths, source), WP(paths, target)
    work = _work_dir(paths, job)
    with _site_lock(paths, source.domain, target.domain):
        try:
            old_home = src.option("home")
            log("Copying files")
            _copy_files(paths, src, dst, keep_config=False)
            job["results"].append({"name": "Files", "ok": True, "message": ""})
            log("Creating a database")
            db = _create_db(paths, target)
            log("Copying the database")
            _copy_database(paths, src, db["name"], work)
            set_define(dst.config, "DB_NAME", db["name"])
            set_define(dst.config, "DB_USER", db["user"])
            set_define(dst.config, "DB_PASSWORD", db["password"])
            job["results"].append({"name": "Database", "ok": True, "message": db["name"]})
            _replace_urls(dst, old_home, source.domain, log)
            if job["mode"] == "staging":
                # Keep the copy out of search engines.
                dst.run("option", "update", "blog_public", "0")
                save_meta(paths, source.domain, staging=target.domain)
                save_meta(paths, target.domain, staging_of=source.domain)
            with contextlib.suppress(ActionError):
                dst.run("cache", "flush")
            with contextlib.suppress(ActionError):
                dst.run("rewrite", "flush")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    job["status"] = "ok"
    job["message"] = f"copied to {_url(target)}"


def run_push(paths: settings.Paths, job: dict[str, Any]) -> None:
    import shutil

    log = backups.Log(paths, job["id"])
    staging = websites._load_site(paths, job["domain"])
    live = websites._load_site(paths, job["target"])
    src, dst = WP(paths, staging), WP(paths, live)
    parts = job["parts"]
    work = _work_dir(paths, job)
    with _site_lock(paths, staging.domain, live.domain):
        try:
            live_db = get_define(dst.config, "DB_NAME") if dst.installed() else None
            if parts.get("database") and not live_db:
                raise ActionError(f"can't read the database name of {live.domain}")
            search_engines = dst.option("blog_public") if live_db else "1"

            # A copy of the live site as it was, in case the push was a mistake.
            safety = live.home / ".tela" / "before-push"
            log(f"Saving the current live site in {safety}")
            safety.mkdir(parents=True, exist_ok=True)
            backups.host().sync(dst.docroot, safety / "public_html", delete=True)
            if live_db:
                backups.host().dump_database(live_db, safety / "database.sql")
            backups.host().chown_tree(live.home / ".tela", "root", "root")
            os.chmod(live.home / ".tela", 0o700)

            if parts.get("files"):
                log("Copying files")
                _copy_files(paths, src, dst, keep_config=True)
                job["results"].append({"name": "Files", "ok": True, "message": ""})
            if parts.get("database"):
                log("Copying the database")
                old_home = src.option("home")
                _copy_database(paths, src, live_db, work)
                _replace_urls(dst, old_home, staging.domain, log)
                dst.run("option", "update", "blog_public", search_engines)
                job["results"].append({"name": "Database", "ok": True, "message": live_db})
            with contextlib.suppress(ActionError):
                dst.run("cache", "flush")
            with contextlib.suppress(ActionError):
                dst.run("rewrite", "flush")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    job["status"] = "ok"
    job["message"] = f"{staging.domain} is now live on {live.domain}"


backups.JOB_KINDS.update(
    {
        "wp-install": run_install,
        "wp-update": run_update,
        "wp-clone": run_clone,
        "wp-push": run_push,
    }
)
