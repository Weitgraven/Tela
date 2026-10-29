# Tela

Tela is a free, open-source web hosting control panel: websites, email, DNS,
databases and backups on your own server, managed from one panel. Every
feature is free. There are no paid add-ons and no premium tier.

> **Status: early development.** Milestones 1–6 (installer, login,
> dashboard, websites with SSL, databases with phpMyAdmin, DNS, email,
> backups) are done. Hosting features are being added milestone by milestone, see the
> [roadmap](#roadmap). Don't run it on a
> production server yet.

## Install

On a fresh **Ubuntu 24.04 or 26.04** server (2 GB memory or more), as root:

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
| `--with-dns-server` | off | Also run Tela's own DNS server (PowerDNS, port 53) |

PHP 8.1, 8.2, 8.3 and 8.4 are installed so each website can choose its own.
Set `TELA_PHP_VERSIONS` to install a different set.

### What gets installed

| Component | Purpose |
| --- | --- |
| OpenLiteSpeed + LSPHP 8.1–8.4 | Web server and PHP |
| Certbot | Free Let's Encrypt certificates, renewed automatically |
| MariaDB | Databases (local connections only) |
| phpMyAdmin | Database management, single sign-on from the panel, port 8091 |
| Postfix | Sending and receiving mail (25, submission on 587 and 465) |
| Dovecot | Mailboxes over IMAP (993, 143) and POP3 (995, 110) |
| Rspamd + Redis | Spam filtering and DKIM signing |
| Roundcube | Webmail on the mail server's host name |
| restic | Encrypted, deduplicated backups, checked every 15 minutes by `tela-backup.timer` |
| Tela panel | Web interface, `tela-panel` service, port 8090 |
| Tela agent | Privileged helper, `tela-agent` service, Unix socket only |

With `--with-dns-server`: PowerDNS as an authoritative DNS server.

Coming in later milestones: Pure-FTPd, nftables and Fail2ban.

## Websites

Add a website under **Websites** in the panel. Each website gets:

- its own Linux user, so PHP from one site can't read another site's files;
- a home folder `/home/<domain>/` with `public_html/` for your files and
  `logs/` for its access and error logs;
- its own PHP version, switchable at any time;
- extra domain names (www is added automatically);
- a free Let's Encrypt certificate once the domain's DNS points at the server,
  and an optional redirect of all visitors to HTTPS.

Tela owns the web server configuration it generates:
`/usr/local/lsws/conf/tela/` and `/usr/local/lsws/conf/vhosts/<domain>/`.
These files are rebuilt on every change, so don't edit them by hand; use
`.htaccess` in `public_html` for per-site rules instead. If the web server
fails to start after a change, Tela restores the previous configuration.

### Upgrading

Run the install command again. It pulls the latest code into `/opt/tela`,
re-applies each step and restarts the services. Your sites, databases,
accounts, configuration and credentials are left alone.

## Databases

Add a database under **Databases** and pick the website it belongs to. Its name
is prefixed with the website's system user (`shop_wordpress`), and a MariaDB user
with the same name gets full rights on that database only. The password is shown
once; Tela doesn't store it, but you can set a new one at any time. Deleting a
website also deletes its databases.

**phpMyAdmin** opens from the database list without asking for a password.
Behind the button, Tela creates a temporary MariaDB user that can only see that
website's databases, and hands its credentials to phpMyAdmin through a link
that works once, within a minute. Temporary users are removed after 12 hours.
phpMyAdmin can't be signed into any other way. It runs on port 8091 (set
`TELA_PMA_PORT` when installing to change it), uses the panel's certificate and
runs PHP as its own `tela-pma` user.

## DNS

Tela manages DNS records in one of two places, chosen per domain:

- **Cloudflare**: for domains that use Cloudflare's nameservers. Add an API
  token with "Zone: Read" and "DNS: Edit" permissions under **DNS → Settings**,
  then add a zone by picking it from your Cloudflare account. Records stay in
  Cloudflare; Tela edits them through the API, including the proxy (orange
  cloud) setting. Removing the zone from Tela leaves it untouched in Cloudflare.
- **Tela DNS**: Tela's own authoritative DNS server (PowerDNS), installed with
  `--with-dns-server`. Set your nameserver names (e.g. `ns1.example.com` and
  `ns2.example.com`) under **DNS → Settings**, register them with their IP at
  your registrar, and point your domains at them. Port 53 (TCP and UDP) must be
  reachable. To free port 53 the installer turns off systemd-resolved's local
  stub listener; the server itself keeps resolving names normally.

Set the server's public IP under **DNS → Settings**. New zones and new websites
then get `@` and `www` A (and AAAA) records pointing at it, unless the names
already have records. Behind a router, use the router's public IP.

## Email

1. **Email → Mail server**: set the mail server's host name, e.g.
   `mail.example.com`, and give it an A record pointing at the server (not
   proxied, if you use Cloudflare). Then
   click **Get certificate** for a Let's Encrypt certificate (port 80 must be
   reachable), so mail apps and webmail don't warn.
2. **Add domain**: Tela creates a DKIM key for it. If the domain has a DNS zone
   in Tela, the MX, SPF, DKIM and DMARC records are added for you; otherwise the
   page lists them to add where your DNS is.
3. Add mailboxes (with an optional size limit) and forwarders. A forwarder
   with `*` as its address is a catch-all for addresses without a mailbox.

Mail apps connect to the host name: IMAP on 993 (SSL/TLS), SMTP on 465
(SSL/TLS) or 587 (STARTTLS), with the full address as user name. Webmail
(Roundcube) is at `https://<mail host name>/`.

Turning off sign-in for a mailbox keeps it receiving mail. Users can add email
for their own websites' domains and manage their mailboxes; the mail server
settings are for administrators.

Things to check, especially on a home connection:

- Many internet providers block outgoing port 25. Then receiving works but
  other servers can't be reached when sending. Ask the provider to unblock it.
- Mail servers distrust home IP addresses. Set a reverse DNS (PTR) record for
  your IP to the mail host name if your provider allows it.
- Forward ports 25, 465, 587 and 993 (and 995, 143, 110 if you use POP3 or plain
  IMAP) to the server.

## Backups

Under **Backups → Add destination**, choose where backups go:

- **S3-compatible storage**: Backblaze B2 (S3 endpoint), Wasabi, Cloudflare R2,
  Amazon S3, MinIO, …
- **Backblaze B2** through its own API
- **SFTP**: any server or NAS you can reach over SSH. Tela shows an SSH key to
  add to that user's `~/.ssh/authorized_keys`.
- **A local folder**, e.g. on a second disk. This doesn't protect against
  losing the server.

Each destination has its own schedule (daily, weekly or by hand), retention
(how many daily, weekly and monthly backups to keep) and selection: website
files, databases, email and the server configuration. Every run makes one
snapshot per website (its files without `logs/`, a dump of each of its
databases, and its mail) plus one of the server configuration (`/etc/tela`,
certificates, mail and DNS settings, the panel database).

Backups are encrypted with a password Tela generates and shows once; **Show
password** shows it again. Keep it somewhere safe: if the server is lost, it's
the only way to read the backups. To use the backups from a new server, add the
same destination there and enter the password under "Connect to an existing
backup repository".

**Restore a website** lists a site's backups from every destination and
restores its files (all, or one file or folder), its databases and its mail.
Restoring files replaces the site's files with the backed-up ones; restoring
mail brings back deleted messages and keeps newer ones. A site's owner can
back up and restore their own sites. Failed backups can send an email.

The server configuration snapshot isn't restored from the panel. To get files
out of it by hand:

```bash
export RESTIC_REPOSITORY='<location shown in the panel>' RESTIC_PASSWORD='<password>'
restic snapshots --tag server
restic restore latest --tag server --target /root/restore
```

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
2. ~~Websites: virtual hosts, PHP versions, Let's Encrypt SSL~~
3. ~~Databases: MariaDB databases and users, phpMyAdmin with single sign-on~~
4. ~~DNS: Cloudflare and PowerDNS, automatic records for websites~~
5. ~~Email: Postfix, Dovecot, Rspamd, DKIM/SPF/DMARC, webmail~~
6. ~~Backups: restic to S3, Backblaze B2 or SFTP, scheduled, with restore~~
7. WordPress: install, staging, cloning, updates
8. Users: client accounts, hosting packages, resellers
9. Firewall and Fail2ban management, two-factor authentication

## License

Tela is licensed under the [GNU General Public License v3.0](LICENSE).
Bundled third-party files: Vue (MIT) and Instrument Sans (SIL Open Font
License, see `web/vendor/fonts/OFL.txt`).

Tela is an independent project inspired by CyberPanel's feature set. It
shares no code with CyberPanel and is not affiliated with it.
