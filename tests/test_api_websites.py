from __future__ import annotations

import pytest

from tela import auth
from tela.db import connect

from .conftest import sign_in


@pytest.fixture
def admin_client(host, client, admin):
    csrf = sign_in(client)
    client.headers["X-CSRF-Token"] = csrf
    return client


def add_site(client, domain="example.com", **extra):
    return client.post("/api/websites", json={"domain": domain, "php_version": "8.3", **extra})


def test_php_versions(admin_client):
    assert admin_client.get("/api/php/versions").json() == {"versions": ["8.2", "8.3"]}


def test_create_and_list(admin_client):
    r = add_site(admin_client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["domain"] == "example.com"
    assert body["owner"]["username"] == "admin"
    assert "ssl_key" not in body

    sites = admin_client.get("/api/websites").json()["websites"]
    assert [s["domain"] for s in sites] == ["example.com"]


def test_agent_validation_errors_reach_the_user(admin_client):
    r = add_site(admin_client, domain="not a domain")
    assert r.status_code == 400
    assert "invalid domain" in r.json()["detail"]


def test_update_php(admin_client):
    add_site(admin_client)
    r = admin_client.patch("/api/websites/example.com", json={"php_version": "8.2"})
    assert r.status_code == 200, r.text
    assert r.json()["php_version"] == "8.2"


def test_issue_ssl(admin_client, host):
    fake, _ = host
    add_site(admin_client)
    fake.dns = {"example.com": {"203.0.113.5"}, "www.example.com": {"203.0.113.5"}}
    r = admin_client.post("/api/websites/example.com/ssl", json={})
    assert r.status_code == 200, r.text
    assert r.json()["ssl_names"] == ["example.com", "www.example.com"]
    r = admin_client.patch("/api/websites/example.com", json={"force_https": True})
    assert r.json()["force_https"] is True


def test_delete_requires_confirmation(admin_client):
    add_site(admin_client)
    r = admin_client.request("DELETE", "/api/websites/example.com", json={"confirm": "nope"})
    assert r.status_code == 400
    r = admin_client.request("DELETE", "/api/websites/example.com", json={"confirm": "example.com"})
    assert r.status_code == 200
    assert admin_client.get("/api/websites").json()["websites"] == []


def test_clients_only_see_their_own_sites(admin_client, client, config):
    add_site(admin_client, "admins.com")
    conn = connect(config.db_path)
    try:
        bob = auth.create_user(conn, "bob", "bobs-long-password", "user")
    finally:
        conn.close()
    add_site(admin_client, "bobs.com", owner_id=bob.id)

    client.cookies.clear()
    csrf = sign_in(client, "bob", "bobs-long-password")
    client.headers["X-CSRF-Token"] = csrf
    sites = client.get("/api/websites").json()["websites"]
    assert [s["domain"] for s in sites] == ["bobs.com"]

    assert client.get("/api/websites/admins.com").status_code == 404
    assert client.patch("/api/websites/admins.com", json={"php_version": "8.2"}).status_code == 404
    assert client.patch("/api/websites/bobs.com", json={"php_version": "8.2"}).status_code == 200
    # Creating and deleting sites is for administrators.
    assert add_site(client, "new.com").status_code == 403
    r = client.request("DELETE", "/api/websites/bobs.com", json={"confirm": "bobs.com"})
    assert r.status_code == 403
