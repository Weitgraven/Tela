from __future__ import annotations

import pytest

from tela import auth
from tela.db import connect

from .conftest import sign_in


@pytest.fixture
def admin_client(host, client, admin):
    client.headers["X-CSRF-Token"] = sign_in(client)
    client.post("/api/websites", json={"domain": "shop.nl", "php_version": "8.3"})
    return client


def test_create_list_password_delete(admin_client):
    r = admin_client.post("/api/databases", json={"domain": "shop.nl", "name": "wp"})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "shop_wp"
    assert len(r.json()["password"]) == 24

    rows = admin_client.get("/api/databases").json()["databases"]
    assert [row["name"] for row in rows] == ["shop_wp"]
    assert "password" not in rows[0]

    r = admin_client.post("/api/databases/shop.nl/shop_wp/password", json={})
    assert r.status_code == 200 and len(r.json()["password"]) == 24

    assert admin_client.delete("/api/databases/shop.nl/shop_wp").status_code == 200
    assert admin_client.get("/api/databases").json()["databases"] == []


def test_bad_name_message(admin_client):
    r = admin_client.post("/api/databases", json={"domain": "shop.nl", "name": "no spaces"})
    assert r.status_code == 400
    assert "lowercase letters" in r.json()["detail"]


def test_phpmyadmin_link(admin_client, host):
    _, paths = host
    paths.pma_root.mkdir()
    (paths.pma_root / "index.php").write_text("<?php")
    admin_client.post("/api/databases", json={"domain": "shop.nl", "name": "wp"})
    r = admin_client.post("/api/databases/shop.nl/phpmyadmin", json={"name": "wp"})
    assert r.status_code == 200, r.text
    assert r.json()["port"] == 8091
    assert r.json()["path"].startswith("/tela-signon.php?token=")


def test_clients_only_manage_their_own(admin_client, client, config):
    conn = connect(config.db_path)
    try:
        bob = auth.create_user(conn, "bob", "bobs-long-password", "user")
    finally:
        conn.close()
    admin_client.post("/api/websites", json={"domain": "bob.nl", "php_version": "8.3",
                                              "owner_id": bob.id})  # fmt: skip
    admin_client.post("/api/databases", json={"domain": "shop.nl", "name": "wp"})

    client.cookies.clear()
    client.headers["X-CSRF-Token"] = sign_in(client, "bob", "bobs-long-password")
    assert client.get("/api/databases").json()["databases"] == []
    r = client.post("/api/databases", json={"domain": "bob.nl", "name": "app"})
    assert r.status_code == 201
    assert [d["name"] for d in client.get("/api/databases").json()["databases"]] == ["bob_app"]
    # Other people's websites look like they don't exist.
    assert client.post("/api/databases", json={"domain": "shop.nl", "name": "x"}).status_code == 404
    assert client.delete("/api/databases/shop.nl/shop_wp").status_code == 404
    assert client.post("/api/databases/shop.nl/phpmyadmin", json={}).status_code == 404
