"""WordPress end to end with real PHP, WP-CLI, MariaDB and a WordPress copy.

Runs only when these are available:

    TELA_TEST_WP_CLI=/path/to/wp-cli.phar
    TELA_TEST_WP_SOURCE=/path/to/wordpress     (an unpacked WordPress)
    MariaDB on /run/mysqld/mysqld.sock, usable as root
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tela_agent import backups, wordpress
from tela_agent.actions import databases, websites
from tela_agent.actions import wordpress as wpa
from tela_agent.mariadb import MariaDB

WP_CLI = os.environ.get("TELA_TEST_WP_CLI")
WP_SOURCE = os.environ.get("TELA_TEST_WP_SOURCE")
SOCKET = Path("/run/mysqld/mysqld.sock")

pytestmark = pytest.mark.skipif(
    not (WP_CLI and WP_SOURCE and shutil.which("php") and SOCKET.exists()),
    reason="needs TELA_TEST_WP_CLI, TELA_TEST_WP_SOURCE, php and MariaDB",
)


class LiveHost(backups.Host):
    def chown_tree(self, path, user, group):  # site users don't exist in tests
        pass


@pytest.fixture
def live(host, monkeypatch):
    fake, paths = host
    monkeypatch.setenv("TELA_WP_CLI", WP_CLI)
    monkeypatch.setenv("TELA_WP_SOURCE", WP_SOURCE)
    monkeypatch.setenv("TELA_WP_PHP", shutil.which("php"))
    monkeypatch.setattr(wordpress, "user_command", lambda user: [])
    real = MariaDB(SOCKET)
    monkeypatch.setattr(databases, "server", lambda paths: real)
    monkeypatch.setattr(backups, "mariadb_server", lambda paths: real)
    monkeypatch.setattr(backups, "host", LiveHost)
    created = []
    yield fake, real, created
    for domain in ("wplive.nl", "staging.wplive.nl", "copy.nl"):
        state = websites._paths().state_dir / f"{domain}.json"
        if state.exists():
            for name in websites._load_site(websites._paths(), domain).databases:
                real.drop_database(name)


def sql(db: str, query: str) -> str:
    return subprocess.run(
        ["mariadb", "-N", db, "-e", query], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_install_staging_push_clone(live):
    _fake, _real, _ = live
    os.umask(0o077)  # like a job started by the agent
    websites.create_site({"domain": "wplive.nl", "php_version": "8.3"})
    job = wpa.install(
        {
            "domain": "wplive.nl",
            "title": "Live site",
            "admin_user": "thomas",
            "admin_email": "t@wplive.nl",
            "password": "a-strong-password-1",
        }
    )
    detail = backups.load_job(websites._paths(), job["id"])
    assert job["status"] == "ok", backups.job_log(websites._paths(), job["id"])
    assert "password" not in str(detail)
    paths = websites._paths()
    site = websites._load_site(paths, "wplive.nl")
    wp = wordpress.WP(paths, site)
    assert wp.installed() and wp.option("home") == "http://wplive.nl"
    # The web server must be able to walk into wp-admin and read static files.
    assert (wp.docroot / "wp-admin").stat().st_mode & 0o777 == 0o755
    css = wp.docroot / "wp-includes" / "css" / "dashicons.min.css"
    assert css.stat().st_mode & 0o777 == 0o644
    assert (wp.docroot / ".htaccess").stat().st_mode & 0o777 == 0o644
    assert wp.config.stat().st_mode & 0o777 == 0o600
    assert wp.option("permalink_structure") == "/%postname%/"
    assert not (site.home / "public_html" / "index.html").exists()
    assert "thomas" in wp.run("user", "list", "--field=user_login")

    listed = {s["domain"]: s for s in wpa.list_sites({})}
    assert listed["wplive.nl"]["installed"] and listed["wplive.nl"]["version"]

    # Staging copy.
    websites.create_site(
        {"domain": "staging.wplive.nl", "php_version": "8.3", "include_www": False}
    )
    job = wpa.clone({"domain": "wplive.nl", "target": "staging.wplive.nl", "mode": "staging"})
    assert job["status"] == "ok", backups.job_log(paths, job["id"])
    stg = wordpress.WP(paths, websites._load_site(paths, "staging.wplive.nl"))
    assert stg.option("home") == "http://staging.wplive.nl"
    assert stg.option("blog_public") == "0"
    assert wordpress.get_define(stg.config, "DB_NAME") != wordpress.get_define(wp.config, "DB_NAME")
    listed = {s["domain"]: s for s in wpa.list_sites({})}
    assert listed["wplive.nl"]["staging"] == "staging.wplive.nl"

    # Change staging, push it live.
    stg.run("post", "create", "--post_title=Made on staging", "--post_status=publish")
    stg.run("option", "update", "blogdescription", "Linking to http://staging.wplive.nl/page")
    (stg.docroot / "new-file.txt").write_text("from staging")
    job = wpa.push({"domain": "staging.wplive.nl"})
    assert job["status"] == "ok", backups.job_log(paths, job["id"])
    assert "Made on staging" in wp.run("post", "list", "--field=post_title")
    assert wp.option("home") == "http://wplive.nl"
    assert wp.option("blog_public") == "1"
    assert wp.option("blogdescription") == "Linking to http://wplive.nl/page"
    assert (wp.docroot / "new-file.txt").exists()
    assert (site.home / ".tela" / "before-push" / "database.sql").exists()

    # Clone to another domain.
    websites.create_site({"domain": "copy.nl", "php_version": "8.3"})
    job = wpa.clone({"domain": "wplive.nl", "target": "copy.nl"})
    assert job["status"] == "ok", backups.job_log(paths, job["id"])
    copy = wordpress.WP(paths, websites._load_site(paths, "copy.nl"))
    assert copy.option("home") == "http://copy.nl"
    assert copy.option("blog_public") == "1"

    info = wpa.info({"domain": "wplive.nl"})
    assert info["version"] and info["staging"] == "staging.wplive.nl"
    assert any(p["name"] for p in info["themes"])
