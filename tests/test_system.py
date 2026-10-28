from __future__ import annotations

from tela import auth
from tela.agent_client import AgentClient, AgentError
from tela.db import connect

from .conftest import running_agent, sign_in


def test_status_requires_login(client, admin):
    assert client.get("/api/system/status").status_code == 401


def test_status_via_agent(client, admin):
    sign_in(client)
    r = client.get("/api/system/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hostname"]
    assert body["memory"]["total"] > 0
    assert {s["unit"] for s in body["services"]} >= {"lsws", "mariadb"}
    assert body["panel_version"]


def test_status_is_admin_only(client, admin, config):
    conn = connect(config.db_path)
    try:
        auth.create_user(conn, "client1", "a-long-enough-password", "user")
    finally:
        conn.close()
    sign_in(client, "client1", "a-long-enough-password")
    assert client.get("/api/system/status").status_code == 403


def test_agent_rejects_unknown_action(agent_socket):
    agent = AgentClient(agent_socket)
    assert agent.call("agent.ping") == "pong"
    try:
        agent.call("rm.everything")
    except AgentError as exc:
        assert "unknown action" in str(exc)
    else:
        raise AssertionError("expected AgentError")


def test_agent_rejects_unlisted_uid(tmp_path):
    with running_agent(tmp_path / "locked.sock", uids=set()) as path:
        try:
            AgentClient(path).call("agent.ping")
        except AgentError as exc:
            assert "permission denied" in str(exc)
        else:
            raise AssertionError("expected permission denied")


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_unknown_api_route_is_json_404(client):
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_frontend_routes_serve_app_shell(client):
    for path in ("/", "/websites", "/email"):
        r = client.get(path)
        assert r.status_code == 200
        assert '<div id="app">' in r.text
    assert client.head("/").status_code == 200
    assert client.get("/static/vendor/vue.esm-browser.prod.js").status_code == 200
