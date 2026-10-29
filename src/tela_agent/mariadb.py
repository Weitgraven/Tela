"""MariaDB administration over the local socket, as root (unix_socket auth).

Identifiers can't be sent as query parameters, so every database and user name
is validated against NAME_RE before it gets here and is then quoted with
backticks. Passwords and user names in strings always go through parameters.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pymysql

from .registry import ActionError

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

SIZES_SQL = (
    "SELECT table_schema, COALESCE(SUM(data_length + index_length), 0)"
    " FROM information_schema.TABLES WHERE table_schema IN ({placeholders})"
    " GROUP BY table_schema"
)


def _ident(name: str) -> str:
    if not NAME_RE.match(name):
        raise ActionError(f"invalid database name: {name!r}")
    return f"`{name}`"


class MariaDB:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def _connect(self):
        try:
            return pymysql.connect(
                unix_socket=str(self.socket_path),
                user="root",
                autocommit=True,
                charset="utf8mb4",
                connect_timeout=10,
            )
        except pymysql.err.OperationalError as exc:
            raise ActionError(f"cannot connect to MariaDB: {exc.args[-1]}") from exc

    def _execute(self, statements: Iterable[tuple[str, tuple]]) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                for sql, args in statements:
                    cur.execute(sql, args)
        except pymysql.MySQLError as exc:
            raise ActionError(f"MariaDB error: {exc.args[-1]}") from exc
        finally:
            conn.close()

    def _query(self, sql: str, args: tuple = ()) -> list[tuple]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall())
        except pymysql.MySQLError as exc:
            raise ActionError(f"MariaDB error: {exc.args[-1]}") from exc
        finally:
            conn.close()

    # -- databases ---------------------------------------------------------
    def database_exists(self, name: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (name,)
        )
        return bool(rows)

    def user_exists(self, user: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM mysql.user WHERE User = %s AND Host = 'localhost'", (user,)
        )
        return bool(rows)

    def create_database(self, name: str, password: str) -> None:
        """Create a database and a user of the same name with full rights on it."""
        db = _ident(name)
        self._execute(
            [
                (f"CREATE DATABASE {db} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci", ()),
                ("CREATE USER %s@'localhost' IDENTIFIED BY %s", (name, password)),
                (f"GRANT ALL PRIVILEGES ON {db}.* TO %s@'localhost'", (name,)),
            ]
        )

    def drop_database(self, name: str) -> None:
        db = _ident(name)
        self._execute(
            [
                (f"DROP DATABASE IF EXISTS {db}", ()),
                ("DROP USER IF EXISTS %s@'localhost'", (name,)),
            ]
        )

    def recreate_database(self, name: str) -> None:
        """Empty a database for a restore. Its user and grants stay."""
        db = _ident(name)
        self._execute(
            [
                (f"DROP DATABASE IF EXISTS {db}", ()),
                (f"CREATE DATABASE {db} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci", ()),
            ]
        )

    def set_password(self, user: str, password: str) -> None:
        _ident(user)
        self._execute([("ALTER USER %s@'localhost' IDENTIFIED BY %s", (user, password))])

    def sizes(self, names: list[str]) -> dict[str, int]:
        if not names:
            return {}
        placeholders = ", ".join(["%s"] * len(names))
        # Only "%s" placeholders are interpolated; the names go in as parameters.
        sql = SIZES_SQL.format(placeholders=placeholders)
        rows = self._query(sql, tuple(names))
        return {str(schema): int(size) for schema, size in rows}

    # -- temporary users for phpMyAdmin single sign-on ---------------------
    def create_temp_user(self, user: str, password: str, databases: list[str]) -> None:
        _ident(user)
        statements: list[tuple[str, tuple]] = [
            ("CREATE USER %s@'localhost' IDENTIFIED BY %s", (user, password))
        ]
        for name in databases:
            statements.append(
                (f"GRANT ALL PRIVILEGES ON {_ident(name)}.* TO %s@'localhost'", (user,))
            )
        self._execute(statements)

    def drop_user(self, user: str) -> None:
        _ident(user)
        self._execute([("DROP USER IF EXISTS %s@'localhost'", (user,))])
