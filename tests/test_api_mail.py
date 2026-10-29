from __future__ import annotations

import pytest

from tela import auth
from tela.db import connect

from .conftest import sign_in


@pytest.fixture
def admin_client(host, client, admin):
    client.headers["X-CSRF-Token"] = sign_in(client)
    r = client.put("/api/mail/settings", json={"hostname": "mail.host.nl"})
    assert r.status_code == 200, r.text
    return client


def test_status_and_settings(admin_client):
    status = admin_client.get("/api/mail/status").json()
    assert status["hostname"] == "mail.host.nl" and status["installed"] is True
    r = admin_client.put("/api/mail/settings", json={"hostname": "host.nl"})
    assert r.status_code == 400 and "host name like" in r.json()["detail"]


def test_domain_mailboxes_aliases(admin_client):
    r = admin_client.post("/api/mail/domains", json={"domain": "Shop.nl", "dns": False})
    assert r.status_code == 201, r.text
    assert r.json()["domain"] == "shop.nl"
    assert admin_client.get("/api/mail/domains").json()["domains"] == [
        {"domain": "shop.nl", "mailboxes": 0, "aliases": 0}
    ]
    r = admin_client.post("/api/mail/domains/shop.nl/mailboxes", json={"local": "info"})
    assert r.status_code == 201 and len(r.json()["password"]) == 20
    r = admin_client.put("/api/mail/domains/shop.nl/mailboxes/info",
                         json={"quota_mb": 100, "enabled": False})  # fmt: skip
    assert r.json()["quota_mb"] == 100 and r.json()["enabled"] is False
    r = admin_client.post("/api/mail/domains/shop.nl/aliases",
                          json={"local": "*", "destinations": ["info@shop.nl"]})  # fmt: skip
    assert r.status_code == 201, r.text
    detail = admin_client.get("/api/mail/domains/shop.nl").json()
    assert [m["address"] for m in detail["mailboxes"]] == ["info@shop.nl"]
    assert detail["aliases"] == [{"local": "*", "destinations": ["info@shop.nl"]}]
    assert admin_client.delete("/api/mail/domains/shop.nl/aliases/%2A").status_code == 200
    assert admin_client.delete("/api/mail/domains/shop.nl/mailboxes/info").status_code == 200
    assert admin_client.delete("/api/mail/domains/shop.nl").status_code == 200
    assert admin_client.get("/api/mail/domains").json()["domains"] == []


def test_users_manage_only_their_own(admin_client, client, config):
    conn = connect(config.db_path)
    try:
        bob = auth.create_user(conn, "bob", "bobs-long-password", "user")
    finally:
        conn.close()
    admin_client.post("/api/websites", json={"domain": "bob.nl", "php_version": "8.3",
                                              "owner_id": bob.id})  # fmt: skip
    admin_client.post("/api/mail/domains", json={"domain": "shop.nl", "dns": False})

    client.headers["X-CSRF-Token"] = sign_in(client, "bob", "bobs-long-password")
    r = client.post("/api/mail/domains", json={"domain": "other.nl"})
    assert r.status_code == 403
    r = client.post("/api/mail/domains", json={"domain": "bob.nl"})
    assert r.status_code == 201, r.text
    assert [d["domain"] for d in client.get("/api/mail/domains").json()["domains"]] == ["bob.nl"]
    assert client.get("/api/mail/domains/shop.nl").status_code == 404
    assert (
        client.post("/api/mail/domains/shop.nl/mailboxes", json={"local": "x"}).status_code == 404
    )
    assert client.post("/api/mail/domains/bob.nl/mailboxes", json={"local": "x"}).status_code == 201
    assert client.put("/api/mail/settings", json={"hostname": "mail.bob.nl"}).status_code == 403
