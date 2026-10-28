"""SQLite storage for panel state, with simple numbered migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Append new migrations to the end. Never edit one that has shipped.
MIGRATIONS: list[str] = [
    # 1: users, sessions, login throttling, audit log
    """
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY,
        username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('admin', 'reseller', 'user')),
        disabled      INTEGER NOT NULL DEFAULT 0,
        created_at    INTEGER NOT NULL,
        last_login_at INTEGER
    );
    CREATE TABLE sessions (
        token_hash   TEXT PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf_token   TEXT NOT NULL,
        created_at   INTEGER NOT NULL,
        expires_at   INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        ip           TEXT,
        user_agent   TEXT
    );
    CREATE INDEX sessions_user ON sessions(user_id);
    CREATE TABLE login_attempts (
        id       INTEGER PRIMARY KEY,
        ts       INTEGER NOT NULL,
        ip       TEXT NOT NULL,
        username TEXT NOT NULL,
        success  INTEGER NOT NULL
    );
    CREATE INDEX login_attempts_ts ON login_attempts(ts);
    CREATE TABLE audit_log (
        id      INTEGER PRIMARY KEY,
        ts      INTEGER NOT NULL,
        user_id INTEGER,
        ip      TEXT,
        action  TEXT NOT NULL,
        detail  TEXT
    );
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(path: Path) -> int:
    """Bring the database up to date. Returns the resulting schema version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
            version = number
        return version
    finally:
        conn.close()
