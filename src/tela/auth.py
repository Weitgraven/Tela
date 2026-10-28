"""Users, password hashing, sessions and login throttling."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import string
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .db import transaction

ROLES = ("admin", "reseller", "user")
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")
MIN_PASSWORD_LENGTH = 12

# Throttling: max failed logins inside the window, per IP and per username.
THROTTLE_WINDOW = 15 * 60
MAX_FAILS_PER_IP = 10
MAX_FAILS_PER_USER = 5

_hasher = PasswordHasher()
# Verified against when the username doesn't exist, so timing doesn't reveal it.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class AuthError(Exception):
    pass


class ThrottledError(AuthError):
    pass


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str

    def public(self) -> dict[str, object]:
        return {"id": self.id, "username": self.username, "role": self.role}


@dataclass(frozen=True)
class Session:
    token: str  # only known at creation time; the DB stores a hash
    csrf_token: str
    user: User


def _now() -> int:
    return int(time.time())


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def validate_username(username: str) -> str:
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise AuthError(
            "username must be 2-32 characters: lowercase letters, digits, '.', '_' or '-', "
            "starting with a letter"
        )
    return username


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str) -> User:
    username = validate_username(username)
    validate_password(password)
    if role not in ROLES:
        raise AuthError(f"role must be one of {', '.join(ROLES)}")
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, _hasher.hash(password), role, _now()),
        )
    except sqlite3.IntegrityError as exc:
        raise AuthError(f"user {username!r} already exists") from exc
    return User(id=int(cur.lastrowid or 0), username=username, role=role)


def set_password(conn: sqlite3.Connection, username: str, password: str) -> None:
    validate_password(password)
    with transaction(conn):
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (_hasher.hash(password), username),
        )
        if cur.rowcount == 0:
            raise AuthError(f"no such user {username!r}")
        # A password change signs out every existing session.
        conn.execute(
            "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)",
            (username,),
        )


def count_users(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def _check_throttle(conn: sqlite3.Connection, ip: str, username: str) -> None:
    since = _now() - THROTTLE_WINDOW
    ip_fails = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND ts > ? AND success = 0",
        (ip, since),
    ).fetchone()[0]
    user_fails = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE username = ? AND ts > ? AND success = 0",
        (username, since),
    ).fetchone()[0]
    if ip_fails >= MAX_FAILS_PER_IP or user_fails >= MAX_FAILS_PER_USER:
        raise ThrottledError("too many failed attempts, try again in 15 minutes")


def login(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    ip: str,
    user_agent: str,
    session_hours: int,
) -> Session:
    username = username.strip().lower()[:64]
    _check_throttle(conn, ip, username)

    row = conn.execute(
        "SELECT id, username, role, password_hash, disabled FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    ok = False
    try:
        _hasher.verify(row["password_hash"] if row else _DUMMY_HASH, password)
        ok = row is not None and not row["disabled"]
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False

    now = _now()
    conn.execute(
        "INSERT INTO login_attempts (ts, ip, username, success) VALUES (?, ?, ?, ?)",
        (now, ip, username, int(ok)),
    )
    # Keep the table small.
    conn.execute("DELETE FROM login_attempts WHERE ts < ?", (now - 7 * 86400,))
    if not ok or row is None:
        raise AuthError("invalid username or password")

    if _hasher.check_needs_rehash(row["password_hash"]):
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_hasher.hash(password), row["id"]),
        )

    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with transaction(conn):
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, expires_at,"
            " last_seen_at, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _hash_token(token),
                row["id"],
                csrf,
                now,
                now + session_hours * 3600,
                now,
                ip,
                user_agent[:300],
            ),
        )
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
    user = User(id=row["id"], username=row["username"], role=row["role"])
    return Session(token=token, csrf_token=csrf, user=user)


def resolve_session(
    conn: sqlite3.Connection, token: str, *, idle_minutes: int
) -> tuple[User, str] | None:
    """Return (user, csrf_token) for a valid session token, or None."""
    if not token or len(token) > 128:
        return None
    now = _now()
    row = conn.execute(
        "SELECT s.token_hash, s.csrf_token, s.expires_at, s.last_seen_at,"
        " u.id, u.username, u.role, u.disabled"
        " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (_hash_token(token),),
    ).fetchone()
    if row is None:
        return None
    if row["disabled"] or row["expires_at"] < now or row["last_seen_at"] + idle_minutes * 60 < now:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    if now - row["last_seen_at"] > 60:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (now, row["token_hash"]),
        )
    user = User(id=row["id"], username=row["username"], role=row["role"])
    return user, row["csrf_token"]


def logout(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def audit(
    conn: sqlite3.Connection, action: str, *, user_id: int | None, ip: str | None, detail: str = ""
) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, user_id, ip, action, detail) VALUES (?, ?, ?, ?, ?)",
        (_now(), user_id, ip, action, detail[:1000]),
    )
