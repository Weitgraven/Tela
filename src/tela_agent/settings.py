"""Filesystem locations the agent manages. Overridable through the environment for tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    ols_root: Path
    sites_root: Path
    state_dir: Path
    letsencrypt: Path

    @property
    def httpd_config(self) -> Path:
        return self.ols_root / "conf" / "httpd_config.conf"

    @property
    def tela_conf_dir(self) -> Path:
        """Directory included from httpd_config.conf; Tela owns everything in it."""
        return self.ols_root / "conf" / "tela"

    @property
    def vhost_conf_root(self) -> Path:
        return self.ols_root / "conf" / "vhosts"

    @property
    def default_cert(self) -> Path:
        return self.ols_root / "conf" / "tela-default.crt"

    @property
    def default_key(self) -> Path:
        return self.ols_root / "conf" / "tela-default.key"


def load() -> Paths:
    env = os.environ.get
    return Paths(
        ols_root=Path(env("TELA_OLS_ROOT", "/usr/local/lsws")),
        sites_root=Path(env("TELA_SITES_ROOT", "/home")),
        state_dir=Path(env("TELA_STATE_DIR", "/etc/tela/sites")),
        letsencrypt=Path(env("TELA_LETSENCRYPT_DIR", "/etc/letsencrypt")),
    )
