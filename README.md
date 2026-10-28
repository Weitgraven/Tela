# Tela

Tela is a free, open-source web hosting control panel: websites, email, DNS,
databases and backups on your own server, managed from one panel. Every
feature is free. There are no paid add-ons and no premium tier.

> **Status: early development.** Milestone 1 (installer, login, server
> dashboard) is done. Hosting features are being added milestone by milestone,
> see the [roadmap](#roadmap). Don't run it on a production server yet.

## Install

On a fresh **Ubuntu 24.04** server (2 GB memory or more), as root:

```bash
curl -sSL https://raw.githubusercontent.com/Weitgraven/Tela/main/install.sh | sudo bash
```

When it finishes, the installer prints the panel address and the admin
password:

```
  Panel     https://203.0.113.10:8090
  Username  admin
  Password  ••••••••••••••••••••
```

The panel uses a self-signed certificate at first, so your browser shows a
warning on the first visit. The credentials are also saved in
`/root/tela-credentials.txt`, readable by root only.

### Options

```bash
curl -sSL https://raw.githubusercontent.com/Weitgraven/Tela/main/install.sh \
  | sudo bash -s -- --hostname server1.example.com --port 8090 --php 8.3
```

| Option | Default | |
| --- | --- | --- |
| `--hostname NAME` | unchanged | Set the server's hostname |
| `--port PORT` | `8090` | Port the panel listens on |
| `--php VERSION` | `8.3` | Default PHP version |
| `--branch NAME` | `main` | Git branch to install |

### What gets installed

| Component | Purpose |
| --- | --- |
| OpenLiteSpeed + LSPHP | Web server and PHP |
| MariaDB | Databases (local connections only) |
| Tela panel | Web interface, `tela-panel` service, port 8090 |
| Tela agent | Privileged helper, `tela-agent` service, Unix socket only |

Coming in later milestones: PowerDNS, Postfix, Dovecot, Rspamd, SnappyMail,
Pure-FTPd, acme.sh, phpMyAdmin, restic, nftables and Fail2ban.

### Upgrading

Run the install command again. It pulls the latest code into `/opt/tela`,
re-applies each step and restarts the services. Your sites, databases,
accounts, configuration and credentials are left alone.

## The `tela` command

```bash
sudo tela user list
sudo tela user add jan --role user --generate     # roles: admin, reseller, user
sudo tela user passwd admin                       # also signs out existing sessions
sudo tela status                                  # server status as JSON
sudo tela version
```

## How it works

```
 browser ──HTTPS──▶ tela-panel ──Unix socket──▶ tela-agent ──▶ OpenLiteSpeed, MariaDB, ...
                   (user "tela",               (root, fixed list
                    sandboxed)                  of actions)
```

The panel runs as the unprivileged `tela` user inside a systemd sandbox and
cannot change the system itself. Anything that needs root (a new site, a
mailbox, a certificate) is requested from the agent over
`/run/tela/agent.sock`. The agent:

- only accepts connections from root and the `tela` user, checked with the
  peer credentials of the socket;
- only runs actions that are registered by name in `src/tela_agent/actions/`,
  each validating its own parameters;
- never runs shell strings.

A bug in the web-facing code therefore can't hand out root access.

Other security defaults:

- Passwords are hashed with Argon2id.
- Session cookies are `HttpOnly`, `Secure` and `SameSite=Strict`.
- Every state-changing request needs a CSRF token.
- Failed logins are throttled per IP and per username.
- The panel sends a strict Content-Security-Policy and loads nothing from
  third parties.
- The OpenLiteSpeed WebAdmin console only listens on 127.0.0.1.

## Repository layout

```
install.sh              entry point; fetches the code and runs install/steps/*
install/lib.sh          installer helpers
install/steps/          one file per install step, run in order
systemd/                service units (installed with @TELA_DIR@ filled in)
src/tela/               panel: FastAPI app, auth, SQLite, CLI
src/tela_agent/         agent: socket server and privileged actions
web/                    frontend: Vue 3 with no build step, self-hosted fonts
tests/                  pytest suite
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest            # tests, including a real agent on a temp socket
.venv/bin/ruff check .      # lint
shellcheck -x install.sh install/lib.sh install/steps/*.sh
```

To run the panel locally without installing anything system-wide:

```bash
mkdir -p .dev && cat > .dev/tela.conf <<'EOF'
[panel]
host = "127.0.0.1"
port = 8090
[paths]
data_dir = ".dev/data"
agent_socket = ".dev/agent.sock"
EOF
export PYTHONPATH=src TELA_CONFIG=.dev/tela.conf
.venv/bin/python -m tela user add admin --role admin --generate
TELA_AGENT_SOCKET=.dev/agent.sock TELA_AGENT_ALLOW_UIDS=$(id -u) .venv/bin/python -m tela_agent &
.venv/bin/python -m tela    # http://127.0.0.1:8090
```

The frontend has no build step: edit `web/app.js` and `web/style.css` and
reload.

## Roadmap

1. ~~Installer, login, server dashboard~~
2. Websites: virtual hosts, PHP versions, Let's Encrypt SSL
3. Databases: MariaDB databases and users, phpMyAdmin with single sign-on
4. DNS: PowerDNS, automatic zones
5. Email: Postfix, Dovecot, Rspamd, DKIM/SPF/DMARC, webmail
6. Backups: restic to S3, Backblaze B2 or SFTP, scheduled, with restore
7. WordPress: install, staging, cloning, updates
8. Users: client accounts, hosting packages, resellers
9. Firewall and Fail2ban management, two-factor authentication

## License

Tela is licensed under the [GNU General Public License v3.0](LICENSE).
Bundled third-party files: Vue (MIT) and Instrument Sans (SIL Open Font
License, see `web/vendor/fonts/OFL.txt`).

Tela is an independent project inspired by CyberPanel's feature set. It
shares no code with CyberPanel and is not affiliated with it.
