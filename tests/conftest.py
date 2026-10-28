from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tela import auth
from tela.app import create_app
from tela.config import REPO_ROOT, Config
from tela.db import connect, migrate
from tela_agent.server import AgentServer

ADMIN_PASSWORD = "correct-horse-battery"


@contextlib.contextmanager
def running_agent(path: Path, uids: set[int]) -> Iterator[Path]:
    """Run a real tela-agent on a temporary socket in a background thread."""
    server = AgentServer(path, group="nonexistent-group", uids=uids)
    loop = asyncio.new_event_loop()
    task = loop.create_task(server.serve())

    def run() -> None:
        asyncio.set_event_loop(loop)
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(task)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.02)
    try:
        yield path
    finally:
        loop.call_soon_threadsafe(task.cancel)
        thread.join(timeout=5)


@pytest.fixture
def agent_socket(tmp_path: Path) -> Iterator[Path]:
    with running_agent(tmp_path / "agent.sock", {os.getuid()}) as path:
        yield path


@pytest.fixture
def config(tmp_path: Path, agent_socket: Path) -> Config:
    return Config(data_dir=tmp_path / "data", agent_socket=agent_socket, web_dir=REPO_ROOT / "web")


@pytest.fixture
def admin(config: Config) -> auth.User:
    migrate(config.db_path)
    conn = connect(config.db_path)
    try:
        return auth.create_user(conn, "admin", ADMIN_PASSWORD, "admin")
    finally:
        conn.close()


@pytest.fixture
def client(config: Config) -> TestClient:
    return TestClient(create_app(config))


def sign_in(client: TestClient, username: str = "admin", password: str = ADMIN_PASSWORD) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf"]
