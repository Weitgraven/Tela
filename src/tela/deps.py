"""FastAPI dependencies: database connection, current user, CSRF, roles."""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from . import auth
from .agent_client import AgentClient
from .config import Config
from .db import connect

SESSION_COOKIE = "tela_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.config.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_agent(request: Request) -> AgentClient:
    return request.app.state.agent


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


Db = Annotated[sqlite3.Connection, Depends(get_db)]
Cfg = Annotated[Config, Depends(get_config)]
Agent = Annotated[AgentClient, Depends(get_agent)]


class Principal:
    def __init__(self, user: auth.User, csrf_token: str, token: str) -> None:
        self.user = user
        self.csrf_token = csrf_token
        self.token = token


def current_principal(request: Request, db: Db, config: Cfg) -> Principal:
    token = request.cookies.get(SESSION_COOKIE, "")
    resolved = auth.resolve_session(db, token, idle_minutes=config.idle_minutes)
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    user, csrf = resolved
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent.encode(), csrf.encode()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
    return Principal(user, csrf, token)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_role(*roles: str):
    def check(principal: CurrentPrincipal) -> Principal:
        if principal.user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed")
        return principal

    return check


AdminPrincipal = Annotated[Principal, Depends(require_role("admin"))]
