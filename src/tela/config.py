"""Panel configuration, read from /etc/tela/tela.conf (TOML) or $TELA_CONFIG."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/tela/tela.conf")
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"  # noqa: S104  (the panel is meant to be reachable)
    port: int = 8090
    tls_cert: Path | None = None
    tls_key: Path | None = None
    data_dir: Path = Path("/var/lib/tela")
    agent_socket: Path = Path("/run/tela/agent.sock")
    web_dir: Path = field(default_factory=lambda: REPO_ROOT / "web")
    session_hours: int = 12
    idle_minutes: int = 120

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tela.sqlite"

    @property
    def tls_enabled(self) -> bool:
        return self.tls_cert is not None and self.tls_key is not None


def _opt_path(value: object) -> Path | None:
    return Path(str(value)) if value else None


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("TELA_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        return Config()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    panel = raw.get("panel", {})
    paths = raw.get("paths", {})
    security = raw.get("security", {})
    defaults = Config()
    return Config(
        host=str(panel.get("host", defaults.host)),
        port=int(panel.get("port", defaults.port)),
        tls_cert=_opt_path(panel.get("tls_cert")),
        tls_key=_opt_path(panel.get("tls_key")),
        data_dir=Path(paths.get("data_dir", defaults.data_dir)),
        agent_socket=Path(paths.get("agent_socket", defaults.agent_socket)),
        web_dir=Path(paths.get("web_dir", defaults.web_dir)),
        session_hours=int(security.get("session_hours", defaults.session_hours)),
        idle_minutes=int(security.get("idle_minutes", defaults.idle_minutes)),
    )
