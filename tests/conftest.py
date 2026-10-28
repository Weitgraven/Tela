from __future__ import annotations

import asyncio
import contextlib
import os
import socket
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
from tela_agent import settings, sysops
from tela_agent.server import AgentServer

from .fakes import FakeSystem

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
    # Wait until the agent accepts connections (the socket file appears just before).
    for _ in range(200):
        if path.exists():
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.connect(str(path))
                break
            except OSError:
                pass
        time.sleep(0.01)
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


STOCK_HTTPD = """\
serverName
user                      nobody
group                     nogroup

listener Default{
  address                 *:8088
  secure                  0
  map                     Example *
}

extprocessor lsphp {
  type                    lsapi
}

virtualhost Example{
  vhRoot                  Example/
  configFile              $SERVER_ROOT/conf/vhosts/$VH_NAME/vhconf.conf
}

module cache {
  ls_enabled              1
}
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    ols_root = tmp_path / "lsws"
    (ols_root / "conf").mkdir(parents=True)
    (ols_root / "conf" / "httpd_config.conf").write_text(STOCK_HTTPD)
    env = {
        "TELA_OLS_ROOT": ols_root,
        "TELA_SITES_ROOT": tmp_path / "home",
        "TELA_STATE_DIR": tmp_path / "state",
        "TELA_LETSENCRYPT_DIR": tmp_path / "letsencrypt",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    fake = FakeSystem()
    fake.letsencrypt = tmp_path / "letsencrypt"
    monkeypatch.setattr(sysops, "system", fake)
    return fake, settings.load()
