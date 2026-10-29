"""The mail services on this machine: Postfix, Dovecot and Rspamd.

Paths and service commands live here so tests can replace the whole thing.
"""

from __future__ import annotations

import base64
import contextlib
import os
import pwd
import re
import shutil
import subprocess
import time
from pathlib import Path

from .registry import ActionError
from .sysops import write_atomic

# Postfix main.cf settings Tela manages. Hostname-dependent values are added by
# postfix_settings().
POSTFIX_SETTINGS = {
    "mydestination": "localhost",
    # Only this machine itself may send without signing in (websites' PHP mail).
    "mynetworks": "127.0.0.0/8 [::1]/128",
    "inet_interfaces": "all",
    "inet_protocols": "all",
    "virtual_mailbox_domains": "texthash:/etc/postfix/tela/domains",
    "virtual_mailbox_maps": "texthash:/etc/postfix/tela/mailboxes",
    "virtual_alias_maps": "texthash:/etc/postfix/tela/aliases",
    "smtpd_sender_login_maps": "texthash:/etc/postfix/tela/senders",
    "virtual_transport": "lmtp:unix:private/dovecot-lmtp",
    "smtpd_tls_cert_file": "/etc/tela/ssl/mail.crt",
    "smtpd_tls_key_file": "/etc/tela/ssl/mail.key",
    "smtpd_tls_security_level": "may",
    "smtpd_tls_auth_only": "yes",
    "smtpd_tls_mandatory_protocols": ">=TLSv1.2",
    "smtpd_tls_protocols": ">=TLSv1.2",
    "smtp_tls_security_level": "may",
    "smtp_tls_loglevel": "1",
    "smtpd_sasl_type": "dovecot",
    "smtpd_sasl_path": "private/auth",
    "smtpd_sasl_auth_enable": "no",
    "smtpd_helo_required": "yes",
    "smtpd_recipient_restrictions": (
        "permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination"
    ),
    "smtpd_milters": "inet:127.0.0.1:11332",
    "non_smtpd_milters": "inet:127.0.0.1:11332",
    "milter_default_action": "accept",
    "milter_protocol": "6",
    "message_size_limit": "52428800",
    "mailbox_size_limit": "0",
    "recipient_delimiter": "+",
    "disable_vrfy_command": "yes",
}

# Authenticated submission: 587 with STARTTLS and 465 with implicit TLS.
SUBMISSION_OVERRIDES = [
    "syslog_name=postfix/{name}",
    "smtpd_tls_security_level=encrypt",
    "smtpd_sasl_auth_enable=yes",
    "smtpd_tls_auth_only=yes",
    "smtpd_client_restrictions=permit_sasl_authenticated,reject",
    "smtpd_sender_restrictions=reject_sender_login_mismatch",
    "smtpd_recipient_restrictions=permit_sasl_authenticated,reject",
    "milter_macro_daemon_name=ORIGINATING",
]


class MailSystem:
    postfix_dir = Path("/etc/postfix/tela")
    dovecot_conf = Path("/etc/dovecot/dovecot.conf")
    dovecot_users = Path("/etc/dovecot/tela/users")
    dkim_dir = Path("/var/lib/rspamd/dkim")
    rspamd_local = Path("/etc/rspamd/local.d")
    vmail_root = Path("/var/vmail")
    cert = Path("/etc/tela/ssl/mail.crt")
    key = Path("/etc/tela/ssl/mail.key")

    # -- discovery ---------------------------------------------------------
    @property
    def installed(self) -> bool:
        return shutil.which("postfix") is not None and shutil.which("dovecot") is not None

    def dovecot_version(self) -> tuple[int, int]:
        try:
            out = subprocess.run(["dovecot", "--version"], capture_output=True, text=True,
                                 timeout=10, check=False).stdout  # fmt: skip
        except OSError:
            return (2, 3)
        match = re.match(r"(\d+)\.(\d+)", out.strip())
        return (int(match.group(1)), int(match.group(2))) if match else (2, 3)

    def vmail_ids(self) -> tuple[int, int]:
        try:
            entry = pwd.getpwnam("vmail")
        except KeyError as exc:
            raise ActionError("the vmail user is missing; run the Tela installer again") from exc
        return entry.pw_uid, entry.pw_gid

    # -- services ----------------------------------------------------------
    @staticmethod
    def _run(args: list[str], what: str) -> str:
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=60,
                                    check=False)  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ActionError(f"{what} failed: {exc}") from exc
        if result.returncode != 0:
            raise ActionError(f"{what} failed: {(result.stderr or result.stdout).strip()}")
        return result.stdout

    def postconf(self, settings: dict[str, str]) -> None:
        self._run(["postconf", "-e", *[f"{k}={v}" for k, v in settings.items()]], "postconf")

    def configure_submission(self) -> None:
        """Enable ports 587 (STARTTLS) and 465 (implicit TLS) for signed-in users."""
        for name, extra in (("submission", []), ("submissions", ["smtpd_tls_wrappermode=yes"])):
            self._run(["postconf", "-M", f"{name}/inet={name} inet n - y - - smtpd"],
                      f"enable {name}")  # fmt: skip
            overrides = [o.format(name=name) for o in SUBMISSION_OVERRIDES] + extra
            self._run(["postconf", "-P", *[f"{name}/inet/{o}" for o in overrides]],
                      f"configure {name}")  # fmt: skip

    @staticmethod
    def ipv6_available() -> bool:
        return Path("/proc/net/if_inet6").exists()

    def reload_postfix(self) -> None:
        self._run(["postfix", "check"], "Postfix configuration check")
        self._run(["systemctl", "reload-or-restart", "postfix"], "Postfix reload")

    def check_dovecot(self) -> None:
        self._run(["doveconf", "-n"], "Dovecot configuration check")

    def reload_dovecot(self) -> None:
        self._run(["systemctl", "reload-or-restart", "dovecot"], "Dovecot reload")

    def restart_dovecot(self) -> None:
        self._run(["systemctl", "restart", "dovecot"], "Dovecot restart")

    def reload_rspamd(self) -> None:
        self._run(["systemctl", "reload-or-restart", "rspamd"], "Rspamd reload")

    # -- files -------------------------------------------------------------
    def write(self, path: Path, content: str, mode: int = 0o644, owner: str | None = None,
              group: str | None = None) -> None:  # fmt: skip
        write_atomic(path, content, mode)
        if os.geteuid() == 0 and (owner or group):
            shutil.chown(path, user=owner, group=group)

    def write_users(self, content: str) -> None:
        """Write Dovecot's passwd-file so Dovecot notices the change.

        Dovecot re-reads the file when its mtime (whole seconds) or size changes.
        Two writes within one second that keep the size (e.g. changing a
        password) would go unnoticed, so the mtime is always moved forward.
        """
        try:
            before = self.dovecot_users.stat().st_mtime
        except FileNotFoundError:
            before = 0.0
        self.write(self.dovecot_users, content, 0o640, owner="root", group="dovecot")
        stamp = max(time.time(), int(before) + 1)
        os.utime(self.dovecot_users, (stamp, stamp))

    # -- DKIM --------------------------------------------------------------
    def dkim_key_path(self, domain: str, selector: str) -> Path:
        return self.dkim_dir / f"{domain}.{selector}.key"

    def create_dkim_key(self, domain: str, selector: str) -> None:
        path = self.dkim_key_path(domain, selector)
        if path.exists():
            return
        self.dkim_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        self._run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                   "-out", str(tmp)], "DKIM key generation")  # fmt: skip
        os.chmod(tmp, 0o640)
        if os.geteuid() == 0:
            with contextlib.suppress(LookupError):
                shutil.chown(tmp, user="_rspamd", group="_rspamd")
        os.replace(tmp, path)

    def dkim_public_key(self, domain: str, selector: str) -> str:
        """Base64 of the DER public key, for the DNS TXT record."""
        der = subprocess.run(
            ["openssl", "pkey", "-in", str(self.dkim_key_path(domain, selector)), "-pubout",
             "-outform", "DER"],
            capture_output=True, timeout=30, check=False,
        )  # fmt: skip
        if der.returncode != 0:
            raise ActionError(f"no DKIM key for {domain}")
        return base64.b64encode(der.stdout).decode()

    def remove_dkim_key(self, domain: str, selector: str) -> None:
        self.dkim_key_path(domain, selector).unlink(missing_ok=True)

    # -- mail storage ------------------------------------------------------
    def mailbox_usage(self, domain: str, local: str) -> int:
        root = self.vmail_root / domain / local
        total = 0
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(dirpath, name)).st_size
        return total

    def remove_mail(self, domain: str, local: str | None = None) -> None:
        target = self.vmail_root / domain / local if local else self.vmail_root / domain
        if target.resolve().is_relative_to(self.vmail_root.resolve()) and target.exists():
            shutil.rmtree(target)

    # -- certificates ------------------------------------------------------
    def use_certificate(self, cert: Path, key: Path) -> None:
        """Point Postfix and Dovecot at a certificate (by symlink)."""
        for link, target in ((self.cert, cert), (self.key, key)):
            tmp = link.with_name(link.name + ".new")
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(target)
            os.replace(tmp, link)


def postfix_settings(hostname: str) -> dict[str, str]:
    return {**POSTFIX_SETTINGS, "myhostname": hostname, "myorigin": hostname}


def rspamd_files(selector_dir: Path) -> dict[str, str]:
    """Rspamd local.d overrides (relative file name -> content)."""
    return {
        "dkim_signing.conf": f"""# Generated by Tela.
enabled = true;
path = "{selector_dir}/$domain.$selector.key";
selector = "tela";
allow_username_mismatch = true;
sign_authenticated = true;
sign_local = true;
use_domain = "header";
use_esld = false;
try_fallback = true;
""",
        "arc.conf": f"""# Generated by Tela.
path = "{selector_dir}/$domain.$selector.key";
selector = "tela";
use_domain = "header";
use_esld = false;
try_fallback = true;
""",
        "redis.conf": '# Generated by Tela.\nservers = "127.0.0.1:6379";\n',
        "milter_headers.conf": """# Generated by Tela.
use = ["x-spam-status", "x-spamd-bar", "authentication-results"];
""",
        "actions.conf": """# Generated by Tela.
reject = 15;
add_header = 6;
greylist = 4;
""",
    }
