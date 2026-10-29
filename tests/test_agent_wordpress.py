from __future__ import annotations

import pytest

from tela_agent.actions import websites
from tela_agent.actions import wordpress as wpa
from tela_agent.registry import ActionError
from tela_agent.wordpress import get_define, php_string, set_define

CONFIG = """<?php
define( 'DB_NAME', 'shop_wp' );
define( 'DB_USER', 'shop_wp' );
define( 'DB_PASSWORD', 'old\\'pw' );
define( 'DB_HOST', 'localhost:/run/mysqld/mysqld.sock' );
$table_prefix = 'wp_';
"""


@pytest.fixture
def sites(host):
    fake, paths = host
    websites.create_site({"domain": "shop.nl", "php_version": "8.3"})
    websites.create_site({"domain": "new.nl", "php_version": "8.3"})
    return fake, paths


def fake_wordpress(paths, domain, version="6.8.3"):
    site = websites._load_site(paths, domain)
    root = site.home / "public_html"
    (root / "wp-includes").mkdir(parents=True, exist_ok=True)
    (root / "wp-includes" / "version.php").write_text(f"<?php\n$wp_version = '{version}';\n")
    (root / "wp-config.php").write_text(CONFIG)
    (root / "index.html").unlink(missing_ok=True)
    return site


def test_defines(tmp_path):
    config = tmp_path / "wp-config.php"
    config.write_text(CONFIG)
    assert get_define(config, "DB_PASSWORD") == "old'pw"
    set_define(config, "DB_PASSWORD", "new'pa\\ss")
    assert get_define(config, "DB_PASSWORD") == "new'pa\\ss"
    assert "define( 'DB_PASSWORD', 'new\\'pa\\\\ss' )" in config.read_text()
    assert get_define(config, "DB_NAME") == "shop_wp"
    with pytest.raises(ActionError, match="AUTH_KEY"):
        set_define(config, "AUTH_KEY", "x")
    assert php_string("a'b") == "'a\\'b'"


def test_list_detects_wordpress(sites):
    _fake, paths = sites
    fake_wordpress(paths, "shop.nl")
    listed = {s["domain"]: s for s in wpa.list_sites({})}
    assert listed["shop.nl"]["installed"] and listed["shop.nl"]["version"] == "6.8.3"
    assert not listed["new.nl"]["installed"]
    wpa.save_meta(paths, "shop.nl", staging="new.nl")
    wpa.save_meta(paths, "new.nl", staging_of="shop.nl")
    listed = {s["domain"]: s for s in wpa.list_sites({})}
    assert listed["shop.nl"]["staging"] == "new.nl" and listed["new.nl"]["staging_of"] == "shop.nl"
    websites.delete_site({"domain": "new.nl"})
    assert wpa.list_sites({})[0]["staging"] is None  # the copy is gone


@pytest.mark.parametrize(
    "params, message",
    [
        ({"title": ""}, "site title"),
        ({"admin_user": "a b"}, "admin user name"),
        ({"admin_email": "nope"}, "email"),
        ({"password": "short"}, "10 to 128"),
        ({"locale": "xx_XX"}, "language"),
    ],
)
def test_install_validation(sites, params, message):
    good = {
        "domain": "shop.nl",
        "title": "Shop",
        "admin_user": "thomas",
        "admin_email": "t@shop.nl",
        "password": "long-enough-password",
    }
    with pytest.raises(ActionError, match=message):
        wpa.install({**good, **params})


def test_install_refuses_non_empty_site(sites):
    _fake, paths = sites
    site = websites._load_site(paths, "shop.nl")
    (site.home / "public_html" / "index.php").write_text("<?php")
    with pytest.raises(ActionError, match="isn't empty"):
        wpa.install(
            {
                "domain": "shop.nl",
                "title": "Shop",
                "admin_user": "thomas",
                "admin_email": "t@shop.nl",
                "password": "long-enough-password",
            }
        )


def test_clone_and_push_checks(sites):
    _fake, paths = sites
    with pytest.raises(ActionError, match="isn't installed"):
        wpa.clone({"domain": "shop.nl", "target": "new.nl"})
    fake_wordpress(paths, "shop.nl")
    target = websites._load_site(paths, "new.nl")
    (target.home / "public_html" / "old.php").write_text("x")
    with pytest.raises(ActionError, match="isn't empty"):
        wpa.clone({"domain": "shop.nl", "target": "new.nl"})
    with pytest.raises(ActionError, match="another website"):
        wpa.clone({"domain": "shop.nl", "target": "shop.nl"})
    with pytest.raises(ActionError, match="isn't a staging copy"):
        wpa.push({"domain": "new.nl"})


def test_install_job_without_wp_cli_fails_cleanly(sites, monkeypatch, tmp_path):
    fake, paths = sites
    monkeypatch.setenv("TELA_WP_CLI", str(tmp_path / "missing.phar"))
    job = wpa.install(
        {
            "domain": "shop.nl",
            "title": "Shop",
            "admin_user": "thomas",
            "admin_email": "t@shop.nl",
            "password": "long-enough-password",
        }
    )
    assert job["status"] == "failed" and "WP-CLI is not installed" in job["message"]
    # The database made for it is removed again, and the password isn't kept.
    assert websites._load_site(paths, "shop.nl").databases == []
    assert not list((paths.backup_dir / "jobs").glob("*.secret"))
