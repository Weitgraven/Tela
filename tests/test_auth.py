from __future__ import annotations

import pytest

from tela import auth
from tela.db import connect

from .conftest import ADMIN_PASSWORD, sign_in


def test_login_and_me(client, admin):
    csrf = sign_in(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"] == {"id": admin.id, "username": "admin", "role": "admin"}
    assert me.json()["csrf"] == csrf


def test_username_is_case_insensitive(client, admin):
    sign_in(client, username="Admin")


def test_wrong_password(client, admin):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_unknown_user_gets_same_message(client, admin):
    wrong_pw = client.post("/api/auth/login", json={"username": "admin", "password": "x"})
    no_user = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert wrong_pw.json() == no_user.json()


def test_session_cookie_flags(client, admin):
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_logout_requires_csrf(client, admin):
    sign_in(client)
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_logout(client, admin):
    csrf = sign_in(client)
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_throttling_locks_username(client, admin):
    for _ in range(auth.MAX_FAILS_PER_USER):
        client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 429


def test_password_change_revokes_sessions(client, admin, config):
    sign_in(client)
    conn = connect(config.db_path)
    try:
        auth.set_password(conn, "admin", "another-long-password")
    finally:
        conn.close()
    assert client.get("/api/auth/me").status_code == 401


def test_disabled_user_cannot_sign_in(client, admin, config):
    conn = connect(config.db_path)
    try:
        conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (admin.id,))
    finally:
        conn.close()
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 401


@pytest.mark.parametrize("name", ["", "a", "1abc", "ab cd", "x" * 33, "root;rm"])
def test_bad_usernames_rejected(name):
    with pytest.raises(auth.AuthError):
        auth.validate_username(name)


def test_short_password_rejected(config):
    from tela.db import migrate

    migrate(config.db_path)
    conn = connect(config.db_path)
    try:
        with pytest.raises(auth.AuthError):
            auth.create_user(conn, "bob", "short", "user")
    finally:
        conn.close()
