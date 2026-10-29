"""Filesystem locations and settings the agent uses. Overridable through the
environment for tests; the [phpmyadmin] and [panel] sections of /etc/tela/tela.conf
are read for the phpMyAdmin listener."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    ols_root: Path
    sites_root: Path
    state_dir: Path
    letsencrypt: Path
    mysql_socket: Path = Path("/run/mysqld/mysqld.sock")
    pma_root: Path = Path("/opt/tela-phpmyadmin")
    pma_tokens: Path = Path("/var/lib/tela-pma/tokens")
    pma_port: int = 8091
    pma_user: str = "tela-pma"
    panel_cert: Path = Path("/etc/tela/ssl/panel.crt")
    panel_key: Path = Path("/etc/tela/ssl/panel.key")
    default_php: str = "8.3"
    pdns_dir: Path = Path("/etc/powerdns/tela")
    mailhost_root: Path = Path("/var/lib/tela-mailhost")
    webmail_root: Path = Path("/opt/tela-webmail")
    webmail_user: str = "tela-webmail"
    backup_staging: Path = Path("/var/lib/tela-backup")
    panel_data: Path = Path("/var/lib/tela")
    wp_cli: Path = Path("/usr/local/lib/tela/wp-cli.phar")

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

    @property
    def pma_installed(self) -> bool:
        return (self.pma_root / "index.php").exists()

    @property
    def dns_dir(self) -> Path:
        return self.state_dir.parent / "dns"

    @property
    def mail_dir(self) -> Path:
        return self.state_dir.parent / "mail"

    @property
    def backup_dir(self) -> Path:
        return self.state_dir.parent / "backups"

    @property
    def wordpress_dir(self) -> Path:
        return self.state_dir.parent / "wordpress"

    @property
    def panel_db(self) -> Path:
        return self.panel_data / "tela.sqlite"

    @property
    def webmail_installed(self) -> bool:
        return (self.webmail_root / "index.php").exists()

    @property
    def sso_users_file(self) -> Path:
        return self.state_dir.parent / "pma-sso-users.json"


def _tela_conf() -> dict:
    path = Path(os.environ.get("TELA_CONFIG", "/etc/tela/tela.conf"))
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load() -> Paths:
    env = os.environ.get
    conf = _tela_conf()
    panel = conf.get("panel", {})
    pma = conf.get("phpmyadmin", {})
    defaults = conf.get("defaults", {})
    panel_paths = conf.get("paths", {})
    base = Paths(
        ols_root=Path(env("TELA_OLS_ROOT", "/usr/local/lsws")),
        sites_root=Path(env("TELA_SITES_ROOT", "/home")),
        state_dir=Path(env("TELA_STATE_DIR", "/etc/tela/sites")),
        letsencrypt=Path(env("TELA_LETSENCRYPT_DIR", "/etc/letsencrypt")),
    )
    return Paths(
        ols_root=base.ols_root,
        sites_root=base.sites_root,
        state_dir=base.state_dir,
        letsencrypt=base.letsencrypt,
        mysql_socket=Path(env("TELA_MYSQL_SOCKET", str(base.mysql_socket))),
        pma_root=Path(env("TELA_PMA_ROOT", pma.get("root", str(base.pma_root)))),
        pma_tokens=Path(env("TELA_PMA_TOKENS", pma.get("tokens", str(base.pma_tokens)))),
        pma_port=int(pma.get("port", base.pma_port)),
        pma_user=str(pma.get("user", base.pma_user)),
        panel_cert=Path(panel.get("tls_cert", str(base.panel_cert))),
        panel_key=Path(panel.get("tls_key", str(base.panel_key))),
        default_php=str(defaults.get("php_version", base.default_php)),
        pdns_dir=Path(env("TELA_PDNS_DIR", str(base.pdns_dir))),
        mailhost_root=Path(env("TELA_MAILHOST_ROOT", str(base.mailhost_root))),
        webmail_root=Path(env("TELA_WEBMAIL_ROOT", str(base.webmail_root))),
        backup_staging=Path(env("TELA_BACKUP_STAGING", str(base.backup_staging))),
        wp_cli=Path(env("TELA_WP_CLI", str(base.wp_cli))),
        panel_data=Path(env("TELA_PANEL_DATA", panel_paths.get("data_dir", str(base.panel_data)))),
    )
