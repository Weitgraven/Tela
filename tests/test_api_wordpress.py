from __future__ import annotations

from tela import auth
from tela.db import connect
from tela_agent.actions import websites

from .conftest import sign_in
from .test_agent_wordpress import fake_wordpress


def setup(client, config):
    client.headers["X-CSRF-Token"] = sign_in(client)
    conn = connect(config.db_path)
    try:
        bob = auth.create_user(conn, "bob", "bobs-long-password", "user")
    finally:
        conn.close()
    client.post(
        "/api/websites", json={"domain": "bob.nl", "php_version": "8.3", "owner_id": bob.id}
    )
    client.post("/api/websites", json={"domain": "shop.nl", "php_version": "8.3"})


def test_staging_needs_wordpress_and_cleans_up(host, client, admin, config):
    setup(client, config)
    r = client.post("/api/wordpress/sites/shop.nl/staging")
    assert r.status_code == 400 and "isn't installed" in r.json()["detail"]
    domains = [s["domain"] for s in client.get("/api/websites").json()["websites"]]
    assert "staging.shop.nl" not in domains


def test_owner_sees_only_own_sites(host, client, admin, config):
    _fake, paths = host
    setup(client, config)
    fake_wordpress(paths, "bob.nl")
    client.headers["X-CSRF-Token"] = sign_in(client, "bob", "bobs-long-password")
    sites = client.get("/api/wordpress/sites").json()["sites"]
    assert [s["domain"] for s in sites] == ["bob.nl"] and sites[0]["installed"]
    assert client.post("/api/wordpress/sites/shop.nl/update", json={}).status_code == 404
    r = client.post("/api/wordpress/sites/bob.nl/clone", json={"target": "new.nl"})
    assert r.status_code == 400 and "create the website" in r.json()["detail"]
    # Staging: the site is created for bob; the copy itself fails here (no WP-CLI).
    job = client.post("/api/wordpress/sites/bob.nl/staging").json()
    assert job["kind"] == "wp-clone" and job["status"] == "failed"
    mine = [s["domain"] for s in client.get("/api/websites").json()["websites"]]
    assert "staging.bob.nl" in mine
    assert websites._load_site(paths, "staging.bob.nl").aliases == []
    jobs = client.get("/api/wordpress/jobs").json()["jobs"]
    assert [j["kind"] for j in jobs] == ["wp-clone"]
    assert client.get("/api/backups/jobs").json()["jobs"] == []
