"""Command-line administration, installed as /usr/local/bin/tela."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from . import __version__, auth
from .agent_client import AgentClient, AgentError
from .config import load
from .db import connect, migrate


def _db():
    config = load()
    migrate(config.db_path)
    return connect(config.db_path)


def cmd_version(_args: argparse.Namespace) -> int:
    print(f"tela {__version__}")
    return 0


def cmd_migrate(_args: argparse.Namespace) -> int:
    config = load()
    print(f"schema version {migrate(config.db_path)}")
    return 0


def _read_password(args: argparse.Namespace) -> tuple[str, bool]:
    """Return (password, generated)."""
    if args.generate:
        return auth.generate_password(), True
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n"), False
    first = getpass.getpass("New password: ")
    if first != getpass.getpass("Repeat password: "):
        raise auth.AuthError("passwords do not match")
    return first, False


def cmd_user_add(args: argparse.Namespace) -> int:
    password, generated = _read_password(args)
    conn = _db()
    try:
        user = auth.create_user(conn, args.username, password, args.role)
        auth.audit(conn, "user.create", user_id=None, ip="cli", detail=user.username)
    finally:
        conn.close()
    print(f"created {user.role} {user.username!r}")
    if generated:
        print(f"password: {password}")
    return 0


def cmd_user_passwd(args: argparse.Namespace) -> int:
    password, generated = _read_password(args)
    conn = _db()
    try:
        auth.set_password(conn, args.username, password)
        auth.audit(conn, "user.passwd", user_id=None, ip="cli", detail=args.username)
    finally:
        conn.close()
    print(f"password changed for {args.username!r}; existing sessions were signed out")
    if generated:
        print(f"password: {password}")
    return 0


def cmd_user_list(_args: argparse.Namespace) -> int:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT username, role, disabled FROM users ORDER BY username"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        flag = " (disabled)" if row["disabled"] else ""
        print(f"{row['username']:<32} {row['role']}{flag}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    config = load()
    print(json.dumps(AgentClient(config.agent_socket).call("system.status"), indent=2))
    return 0


def _add_password_opts(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--generate", action="store_true", help="generate a random password")
    group.add_argument("--password-stdin", action="store_true", help="read password from stdin")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tela", description="Tela hosting panel")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version").set_defaults(fn=cmd_version)
    sub.add_parser("migrate", help="update the panel database").set_defaults(fn=cmd_migrate)
    sub.add_parser("status", help="show server status via the agent").set_defaults(fn=cmd_status)

    user = sub.add_parser("user", help="manage panel users")
    user_sub = user.add_subparsers(dest="user_command", required=True)

    add = user_sub.add_parser("add", help="create a user")
    add.add_argument("username")
    add.add_argument("--role", choices=auth.ROLES, default="user")
    _add_password_opts(add)
    add.set_defaults(fn=cmd_user_add)

    passwd = user_sub.add_parser("passwd", help="change a user's password")
    passwd.add_argument("username")
    _add_password_opts(passwd)
    passwd.set_defaults(fn=cmd_user_passwd)

    user_sub.add_parser("list", help="list users").set_defaults(fn=cmd_user_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (auth.AuthError, AgentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
