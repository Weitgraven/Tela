from __future__ import annotations

import shutil

import pytest

from tela import auth
from tela.db import connect

from .conftest import sign_in

pytestmark = pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")


@pytest.fixture
def setup(host, client, admin, config, tmp_path_factory):
    client.headers["X-CSRF-Token"] = sign_in(client)
    conn = connect(config.db_path)
    try:
        bob = auth.create_user(conn, "bob", "bobs-long-password", "user")
    finally:
        conn.close()
    client.post("/api/websites", json={"domain": "bob.nl", "php_version": "8.3",
                                       "owner_id": bob.id})  # fmt: skip
    client.post("/api/websites", json={"domain": "shop.nl", "php_version": "8.3"})
    repo = tmp_path_factory.mktemp("repo") / "r"
    r = client.post("/api/backups/destinations",
                    json={"name": "Disk", "type": "local", "path": str(repo)})  # fmt: skip
    assert r.status_code == 201, r.text
    return client, r.json()


def test_admin_flow(setup):
    client, dest = setup
    assert len(dest["repo_password"]) == 32
    status = client.get("/api/backups/status").json()
    assert status["destinations"][0]["name"] == "Disk"
    assert "repo_password" not in status["destinations"][0]
    schedule = {"frequency": "weekly", "time": "04:15", "weekday": 0}
    r = client.put(f"/api/backups/destinations/{dest['id']}", json={"schedule": schedule})
    assert r.json()["schedule"] == {"frequency": "weekly", "time": "04:15", "weekday": 0}
    job = client.post(f"/api/backups/destinations/{dest['id']}/run").json()
    assert job["status"] == "ok"
    detail = client.get(f"/api/backups/jobs/{job['id']}").json()
    assert "Finished: ok" in detail["log"]
    pw = client.post(f"/api/backups/destinations/{dest['id']}/password").json()
    assert pw["repo_password"] == dest["repo_password"]
    assert client.delete(f"/api/backups/destinations/{dest['id']}").status_code == 200


def test_owner_can_back_up_and_restore_own_site_only(setup, client):
    _client, dest = setup
    client.headers["X-CSRF-Token"] = sign_in(client, "bob", "bobs-long-password")
    status = client.get("/api/backups/status").json()
    assert status["destinations"] == [{"id": dest["id"], "name": "Disk"}]
    assert client.post(f"/api/backups/destinations/{dest['id']}/run").status_code == 403
    assert client.post("/api/backups/sites/shop.nl/run", json={}).status_code == 404
    job = client.post("/api/backups/sites/bob.nl/run", json={}).json()
    assert job["status"] == "ok" and job["domains"] == ["bob.nl"]
    snaps = client.get("/api/backups/sites/bob.nl/snapshots").json()["snapshots"]
    assert len(snaps) == 1
    assert client.get("/api/backups/sites/shop.nl/snapshots").status_code == 404
    body = {"destination": dest["id"], "snapshot": snaps[0]["id"], "files": True}
    r = client.post("/api/backups/sites/bob.nl/restore", json=body)
    assert r.status_code == 202 and r.json()["status"] == "ok", r.text
    jobs = client.get("/api/backups/jobs").json()["jobs"]
    assert {j["kind"] for j in jobs} == {"backup", "restore"}
