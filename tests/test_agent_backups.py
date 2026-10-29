from __future__ import annotations

import shutil
from datetime import datetime

import pytest

from tela_agent import backups
from tela_agent.actions import backups as actions
from tela_agent.actions import databases, mail, websites
from tela_agent.registry import ActionError

needs_restic = pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")


@pytest.fixture
def repo_dir(tmp_path_factory):
    # Outside the test's own tree, which the server snapshot backs up.
    return tmp_path_factory.mktemp("repo") / "tela"


@pytest.fixture
def site(host):
    fake, paths = host
    websites.create_site({"domain": "shop.nl", "php_version": "8.3"})
    site = websites._load_site(paths, "shop.nl")
    (site.home / "public_html" / "index.php").write_text("<?php echo 'v1';")
    (site.home / "public_html" / "uploads").mkdir()
    (site.home / "public_html" / "uploads" / "photo.jpg").write_bytes(b"jpeg-v1")
    (site.home / "logs" / "access.log").write_text("not backed up")
    db = databases.create_database({"domain": "shop.nl", "name": "wp"})["name"]
    fake.mariadb.contents[db] = "INSERT INTO posts VALUES ('v1');"
    return fake, paths, site, db


def make_dest(repo_dir, **extra):
    params = {"type": "local", "path": str(repo_dir), "name": "Local disk",
              "schedule": {"frequency": "daily", "time": "03:00"}, **extra}  # fmt: skip
    return actions.create_destination(params)


# ---------------------------------------------------------------------------
# Validation and schedule (no restic needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params, message",
    [
        ({"type": "ftp"}, "choose where"),
        ({"type": "local", "path": "relative/dir", "name": "x"}, "full path"),
        ({"type": "local", "path": "/etc", "name": "x"}, "dedicated folder"),
        ({"type": "s3", "bucket": "UPPER", "name": "x"}, "bucket"),
        ({"type": "s3", "bucket": "my-bucket", "name": "x", "access_key": "AK"}, "secret key"),
        (
            {
                "type": "sftp",
                "host": "box.example.com",
                "user": "bad user",
                "path": "x",
                "name": "x",
            },
            "user name",
        ),  # fmt: skip
        (
            {
                "type": "local",
                "path": "/backup/x",
                "name": "x",
                "schedule": {"frequency": "daily", "time": "25:00"},
            },
            "HH:MM",
        ),  # fmt: skip
        (
            {
                "type": "local",
                "path": "/backup/x",
                "name": "x",
                "include": {"files": False, "databases": False, "mail": False, "server": False},
            },
            "at least one",
        ),  # fmt: skip
    ],
)
def test_destination_validation(host, params, message):
    with pytest.raises(ActionError, match=message):
        actions.create_destination(params)


def test_repository_locations():
    from tela_agent.restic import Repo

    def loc(**d):
        return Repo({"repo_password": "x", **d}).location

    assert loc(type="s3", endpoint="s3.eu-central-003.backblazeb2.com", bucket="b",
               prefix="srv1") == "s3:https://s3.eu-central-003.backblazeb2.com/b/srv1"  # fmt: skip
    assert loc(type="b2", bucket="my-bucket", prefix="") == "b2:my-bucket:tela"
    assert loc(type="sftp", user="u", host="nas.lan", path="backups/tela", port=2222) \
        == "sftp:u@nas.lan:backups/tela"  # fmt: skip
    opts = Repo({"type": "sftp", "user": "u", "host": "nas.lan", "path": "b", "port": 2222,
                 "repo_password": "x"})._options()  # fmt: skip
    assert opts[1].startswith("sftp.command=ssh -p 2222 ") and opts[1].endswith("u@nas.lan -s sftp")


def test_schedule():
    dest = {"schedule": {"frequency": "daily", "time": "03:00"}, "created_at": 0, "last": None}
    now = datetime(2026, 10, 29, 2, 0)
    assert backups.last_slot(dest["schedule"], now) == datetime(2026, 10, 28, 3, 0)
    assert backups.is_due(dest, now)
    dest["last"] = {"scheduled_at": int(datetime(2026, 10, 28, 3, 0).timestamp())}
    assert not backups.is_due(dest, now)
    assert backups.is_due(dest, datetime(2026, 10, 29, 3, 5))
    weekly = {"frequency": "weekly", "time": "04:30", "weekday": 6}  # Sunday
    assert backups.last_slot(weekly, datetime(2026, 10, 29, 12, 0)) == datetime(2026, 10, 25, 4, 30)
    assert backups.last_slot({"frequency": "manual"}, now) is None
    # A new destination doesn't run a backup for a slot from before it existed.
    fresh = {"schedule": {"frequency": "daily", "time": "03:00"},
             "created_at": int(datetime(2026, 10, 29, 1, 0).timestamp())}  # fmt: skip
    assert not backups.is_due(fresh, datetime(2026, 10, 29, 2, 0))


# ---------------------------------------------------------------------------
# With a real restic repository
# ---------------------------------------------------------------------------


@needs_restic
def test_create_destination_and_secrets(host, repo_dir):
    dest = make_dest(repo_dir)
    assert len(dest["repo_password"]) == 32
    assert (repo_dir / "config").exists()
    status = actions.status({})
    assert "repo_password" not in status["destinations"][0]
    assert status["destinations"][0]["next_run"]
    with pytest.raises(ActionError, match="already uses this location"):
        make_dest(repo_dir)
    # The same repository can be connected again with its password (new server).
    actions.delete_destination({"id": dest["id"]})
    with pytest.raises(ActionError, match="enter its password"):
        make_dest(repo_dir)
    with pytest.raises(ActionError, match="doesn't open"):
        make_dest(repo_dir, repo_password="wrong-password-123")
    again = make_dest(repo_dir, repo_password=dest["repo_password"])
    assert again["connected_existing"] is True and "repo_password" not in again


@needs_restic
def test_backup_and_restore_site(site, repo_dir):
    fake, paths, s, db = site
    dest = make_dest(repo_dir, keep={"daily": 3, "weekly": 0, "monthly": 0})
    job = actions.run({"destination": dest["id"]})
    assert job["status"] == "ok", actions.job({"id": job["id"]})["log"]
    names = [r["name"] for r in job["results"]]
    assert names == ["shop.nl", "Server configuration"]
    assert not (paths.backup_staging / "sites" / "shop.nl").exists()  # dumps cleaned up

    snaps = actions.snapshots({"domain": "shop.nl"})["snapshots"]
    assert len(snaps) == 1 and snaps[0]["destination_name"] == "Local disk"

    # Break things, then restore everything.
    (s.home / "public_html" / "index.php").write_text("<?php echo 'broken';")
    (s.home / "public_html" / "uploads" / "photo.jpg").unlink()
    (s.home / "public_html" / "malware.php").write_text("<?php evil();")
    (s.home / "logs" / "access.log").write_text("new log")
    fake.mariadb.contents[db] = "DROP TABLE posts;"
    restore = actions.restore({"domain": "shop.nl", "destination": dest["id"],
                               "snapshot": snaps[0]["id"],
                               "parts": {"files": True, "databases": True}})  # fmt: skip
    assert restore["status"] == "ok", actions.job({"id": restore["id"]})["log"]
    assert (s.home / "public_html" / "index.php").read_text() == "<?php echo 'v1';"
    assert (s.home / "public_html" / "uploads" / "photo.jpg").read_bytes() == b"jpeg-v1"
    assert not (s.home / "public_html" / "malware.php").exists()
    assert (s.home / "logs" / "access.log").read_text() == "new log"  # logs untouched
    assert (s.home / ".acme").exists()
    assert fake.mariadb.contents[db] == "INSERT INTO posts VALUES ('v1');"
    assert any(path.endswith("public_html") for path, user in fake.backup_host.chowned)


@needs_restic
def test_restore_single_file(site, repo_dir):
    _fake, _paths, s, _db = site
    dest = make_dest(repo_dir)
    actions.run({"destination": dest["id"], "domains": ["shop.nl"]})
    snap = actions.snapshots({"domain": "shop.nl"})["snapshots"][0]
    (s.home / "public_html" / "uploads" / "photo.jpg").write_bytes(b"changed")
    (s.home / "public_html" / "index.php").write_text("keep me")
    job = actions.restore({"domain": "shop.nl", "destination": dest["id"], "snapshot": snap["id"],
                           "parts": {"files": True},
                           "path": "public_html/uploads/photo.jpg"})  # fmt: skip
    assert job["status"] == "ok", actions.job({"id": job["id"]})["log"]
    assert (s.home / "public_html" / "uploads" / "photo.jpg").read_bytes() == b"jpeg-v1"
    assert (s.home / "public_html" / "index.php").read_text() == "keep me"
    with pytest.raises(ActionError, match="invalid path"):
        actions.restore({"domain": "shop.nl", "destination": dest["id"], "snapshot": snap["id"],
                         "parts": {"files": True}, "path": "../../etc/passwd"})  # fmt: skip


@needs_restic
def test_mail_is_backed_up_and_restored(site, repo_dir):
    fake, paths, s, _db = site
    mail.update_settings({"hostname": "mail.host.nl"})
    mail.create_domain({"domain": "shop.nl", "dns": False})
    mail.create_mailbox({"domain": "shop.nl", "local": "info"})
    inbox = fake.mail.vmail_root / "shop.nl" / "info" / "Maildir" / "cur"
    inbox.mkdir(parents=True)
    (inbox / "1.eml").write_text("Subject: hello")
    dest = make_dest(repo_dir)
    actions.run({"destination": dest["id"], "domains": ["shop.nl"]})
    snap = actions.snapshots({"domain": "shop.nl"})["snapshots"][0]
    assert snap["has_mail"]
    (inbox / "1.eml").unlink()
    (inbox / "2.eml").write_text("Subject: newer")
    job = actions.restore({"domain": "shop.nl", "destination": dest["id"], "snapshot": snap["id"],
                           "parts": {"mail": True}})  # fmt: skip
    assert job["status"] == "ok", actions.job({"id": job["id"]})["log"]
    assert (inbox / "1.eml").exists() and (inbox / "2.eml").exists()
    assert ("{}".format(fake.mail.vmail_root / "shop.nl"), "vmail") in fake.backup_host.chowned


@needs_restic
def test_scheduled_run_and_failure_notice(site, repo_dir, monkeypatch):
    fake, paths, _s, _db = site
    dest = make_dest(repo_dir, notify_email="admin@host.nl")
    backups.update_destination(paths, dest["id"], created_at=0)
    ran = backups.run_scheduled(paths, datetime(2030, 1, 1, 3, 30))
    assert len(ran) == 1
    assert backups.run_scheduled(paths, datetime(2030, 1, 1, 3, 45)) == []  # once per slot
    assert backups.load_destination(paths, dest["id"])["last"]["status"] == "ok"
    assert fake.backup_host.mails == []

    def broken(name, target):
        raise ActionError("dump of database shop_wp failed: access denied")

    monkeypatch.setattr(fake.backup_host, "dump_database", broken)
    ran = backups.run_scheduled(paths, datetime(2030, 1, 2, 3, 30))
    job = backups.load_job(paths, ran[0])
    assert job["status"] == "partial" and job["message"] == "1 of 2 failed"
    to, subject, body = fake.backup_host.mails[0]
    assert to == "admin@host.nl" and "partial" in subject and "access denied" in body


@needs_restic
def test_missing_repository_fails_cleanly(site, repo_dir):
    dest = make_dest(repo_dir)
    shutil.rmtree(repo_dir)
    job = actions.run({"destination": dest["id"]})
    assert job["status"] == "failed" and "repository is gone" in job["message"]
