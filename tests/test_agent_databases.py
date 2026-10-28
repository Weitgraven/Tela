from __future__ import annotations

import json
import time

import pytest

from tela_agent.actions import databases, websites
from tela_agent.registry import ActionError


@pytest.fixture
def site(host):
    websites.create_site({"domain": "shop.nl", "php_version": "8.3"})
    return host


def create(name="wp", domain="shop.nl", **extra):
    return databases.create_database({"domain": domain, "name": name, **extra})


def test_create_prefixes_name_and_returns_password(site):
    fake, paths = site
    db = create()
    assert db["name"] == "shop_wp" and db["user"] == "shop_wp"
    assert len(db["password"]) == 24
    assert "shop_wp" in fake.mariadb.databases
    state = json.loads((paths.state_dir / "shop.nl.json").read_text())
    assert state["databases"] == ["shop_wp"]


def test_full_name_is_accepted_too(site):
    assert create("shop_blog")["name"] == "shop_blog"


@pytest.mark.parametrize("bad", ["", "x;drop", "a b", "Wp`", "__x", "x" * 60, "wp-2"])
def test_invalid_names(site, bad):
    with pytest.raises(ActionError):
        create(bad)


def test_duplicate(site):
    create()
    with pytest.raises(ActionError, match="already exists"):
        create()


def test_given_password_is_validated(site):
    with pytest.raises(ActionError, match="12 to 128"):
        create(password="short")
    assert create(password="a-long-enough-pass")["password"] == "a-long-enough-pass"


def test_list_with_sizes(site):
    fake, _ = site
    create()
    fake.mariadb.databases["shop_wp"] = 2048
    assert databases.list_databases({}) == [
        {"name": "shop_wp", "user": "shop_wp", "domain": "shop.nl", "size": 2048}
    ]
    assert databases.list_databases({"domain": "shop.nl"})[0]["name"] == "shop_wp"


def test_change_password(site):
    fake, _ = site
    create()
    result = databases.change_password({"domain": "shop.nl", "name": "wp"})
    assert fake.mariadb.users["shop_wp"][0] == result["password"]


def test_cannot_touch_another_sites_database(site):
    websites.create_site({"domain": "other.nl", "php_version": "8.3"})
    create("secret", domain="other.nl")
    with pytest.raises(ActionError, match="no database"):
        databases.change_password({"domain": "shop.nl", "name": "other_secret"})
    with pytest.raises(ActionError, match="no database"):
        databases.delete_database({"domain": "shop.nl", "name": "other_secret"})


def test_delete(site):
    fake, _ = site
    create()
    databases.delete_database({"domain": "shop.nl", "name": "wp"})
    assert fake.mariadb.databases == {}
    assert databases.list_databases({}) == []


def test_deleting_website_drops_databases(site):
    fake, _ = site
    create()
    websites.delete_site({"domain": "shop.nl"})
    assert fake.mariadb.databases == {}


def test_deleting_website_can_keep_databases(site):
    fake, _ = site
    create()
    result = websites.delete_site({"domain": "shop.nl", "remove_files": False})
    assert result["kept_databases"] == ["shop_wp"]
    assert "shop_wp" in fake.mariadb.databases


def test_signon_requires_phpmyadmin(site):
    create()
    with pytest.raises(ActionError, match="not installed"):
        databases.phpmyadmin_signon({"domain": "shop.nl"})


def test_signon_creates_scoped_temp_user_and_token(site):
    fake, paths = site
    paths.pma_root.mkdir()
    (paths.pma_root / "index.php").write_text("<?php")
    create()
    create("blog")
    websites.create_site({"domain": "other.nl", "php_version": "8.3"})
    create("secret", domain="other.nl")

    result = databases.phpmyadmin_signon({"domain": "shop.nl", "name": "wp"})
    assert result["port"] == 8091
    token = result["path"].split("token=")[1]
    data = json.loads((paths.pma_tokens / f"{token}.json").read_text())
    assert data["db"] == "shop_wp"
    assert data["expires"] > time.time()
    password, grants = fake.mariadb.users[data["user"]]
    assert password == data["password"]
    assert sorted(grants) == ["shop_blog", "shop_wp"]  # never other.nl's database


def test_old_signon_users_expire(site):
    fake, paths = site
    paths.pma_root.mkdir()
    (paths.pma_root / "index.php").write_text("<?php")
    create()
    first = databases.phpmyadmin_signon({"domain": "shop.nl"})
    users = json.loads(paths.sso_users_file.read_text())
    old_user = next(iter(users))
    paths.sso_users_file.write_text(json.dumps({old_user: 0}))
    databases.phpmyadmin_signon({"domain": "shop.nl"})
    assert old_user not in fake.mariadb.users
    assert first  # the first link was handed out


def test_signon_without_databases(site):
    _, paths = site
    paths.pma_root.mkdir()
    (paths.pma_root / "index.php").write_text("<?php")
    with pytest.raises(ActionError, match="no databases"):
        databases.phpmyadmin_signon({"domain": "shop.nl"})
