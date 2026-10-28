from __future__ import annotations

import pytest

from tela import auth
from tela.db import connect

from .conftest import sign_in


@pytest.fixture
def admin_client(host, client, admin):
    client.headers["X-CSRF-Token"] = sign_in(client)
    settings = {"public_ipv4": "203.0.113.5", "nameservers": ["ns1.host.nl", "ns2.host.nl"]}
    r = client.put("/api/dns/settings", json=settings)
    assert r.status_code == 200, r.text
    return client


def test_zone_and_records(admin_client):
    r = admin_client.post("/api/dns/zones", json={"zone": "shop.nl", "provider": "local"})
    assert r.status_code == 201, r.text
    zones = admin_client.get("/api/dns/zones").json()["zones"]
    assert zones == [{"zone": "shop.nl", "provider": "local", "records": 2}]

    r = admin_client.post("/api/dns/zones/shop.nl/records",
                          json={"type": "MX", "content": "mx.shop.nl", "priority": 10})  # fmt: skip
    assert r.status_code == 201, r.text
    record = r.json()
    r = admin_client.put(f"/api/dns/zones/shop.nl/records/{record['id']}",
                         json={"type": "MX", "content": "mx.shop.nl", "priority": 20})  # fmt: skip
    assert r.json()["priority"] == 20
    assert admin_client.delete(f"/api/dns/zones/shop.nl/records/{record['id']}").status_code == 200
    assert admin_client.delete("/api/dns/zones/shop.nl").status_code == 200
    assert admin_client.get("/api/dns/zones").json()["zones"] == []


def test_invalid_record_message(admin_client):
    admin_client.post("/api/dns/zones", json={"zone": "shop.nl", "provider": "local"})
    r = admin_client.post("/api/dns/zones/shop.nl/records", json={"type": "A", "content": "x"})
    assert r.status_code == 400 and "IPv4" in r.json()["detail"]


def test_token_never_returned(admin_client):
    r = admin_client.put("/api/dns/settings", json={"cloudflare_token": "good-token"})
    assert "good-token" not in r.text
    assert "good-token" not in admin_client.get("/api/dns/status").text


def test_client_manages_only_own_zone(admin_client, client, config):
    conn = connect(config.db_path)
    try:
        bob = auth.create_user(conn, "bob", "bobs-long-password", "user")
    finally:
        conn.close()
    admin_client.post("/api/websites", json={"domain": "bob.nl", "php_version": "8.3",
                                              "owner_id": bob.id})  # fmt: skip
    admin_client.post("/api/dns/zones", json={"zone": "bob.nl", "provider": "local"})
    admin_client.post("/api/dns/zones", json={"zone": "shop.nl", "provider": "local"})

    client.cookies.clear()
    client.headers["X-CSRF-Token"] = sign_in(client, "bob", "bobs-long-password")
    assert [z["zone"] for z in client.get("/api/dns/zones").json()["zones"]] == ["bob.nl"]
    r = client.post("/api/dns/zones/bob.nl/records", json={"type": "TXT", "content": "hi"})
    assert r.status_code == 201
    assert client.get("/api/dns/zones/shop.nl/records").status_code == 404
    new_zone = {"zone": "x.nl", "provider": "local"}
    assert client.post("/api/dns/zones", json=new_zone).status_code == 403
    assert client.put("/api/dns/settings", json={"public_ipv4": "1.1.1.1"}).status_code == 403
