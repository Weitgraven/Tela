# shellcheck shell=bash
step "Installing webmail (Roundcube)"

WEBMAIL_ROOT="/opt/tela-webmail"
WEBMAIL_USER="tela-webmail"
WEBMAIL_DATA="/var/lib/tela-webmail"
ROUNDCUBE_FALLBACK="1.7.5"

if ! id "$WEBMAIL_USER" >/dev/null 2>&1; then
  run useradd --system --user-group --home-dir "$WEBMAIL_DATA" --no-create-home \
    --shell /usr/sbin/nologin "$WEBMAIL_USER"
fi
install -d -m 0750 -o "$WEBMAIL_USER" -g "$WEBMAIL_USER" "$WEBMAIL_DATA"
for sub in db temp logs sessions; do
  install -d -m 0700 -o "$WEBMAIL_USER" -g "$WEBMAIL_USER" "$WEBMAIL_DATA/$sub"
done

# Latest release from GitHub, with the SHA-256 GitHub publishes for its files.
release="$(curl -fsSL https://api.github.com/repos/roundcube/roundcubemail/releases/latest || true)"
read -r rc_version rc_url rc_digest < <(
  printf '%s' "$release" | python3 -c '
import json, sys
try:
    r = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
tag = r.get("tag_name", "")
for a in r.get("assets", []):
    if a["name"] == f"roundcubemail-{tag}-complete.tar.gz":
        print(tag, a["browser_download_url"], (a.get("digest") or "-").removeprefix("sha256:"))
        break
' || true
) || true
if [[ ! "${rc_version:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  rc_version="$ROUNDCUBE_FALLBACK"
  rc_url="https://github.com/roundcube/roundcubemail/releases/download/$rc_version/roundcubemail-$rc_version-complete.tar.gz"
  rc_digest="-"
  echo "GitHub API unavailable; using Roundcube $rc_version" >>"$LOG_FILE"
fi

installed_version="$(cat "$WEBMAIL_ROOT/.tela-version" 2>/dev/null || true)"
if [[ "$installed_version" != "$rc_version" ]]; then
  work="$(mktemp -d)"
  run curl -fsSL "$rc_url" -o "$work/roundcube.tar.gz"
  if [[ "$rc_digest" != "-" ]]; then
    actual="$(sha256sum "$work/roundcube.tar.gz" | awk '{print $1}')"
    [[ "$actual" == "$rc_digest" ]] || die "Roundcube download failed its checksum"
  fi
  run tar -xzf "$work/roundcube.tar.gz" -C "$work"
  new="$work/roundcubemail-$rc_version"
  [[ -f "$new/public_html/index.php" ]] || die "unexpected Roundcube archive layout"
  rm -rf "$new/installer" "$new/public_html/installer.php"
  if [[ -f "$WEBMAIL_ROOT/config/config.inc.php" ]]; then
    cp -p "$WEBMAIL_ROOT/config/config.inc.php" "$new/config/config.inc.php"
  fi
  echo "$rc_version" >"$new/.tela-version"
  rm -rf "$WEBMAIL_ROOT.old"
  if [[ -d "$WEBMAIL_ROOT" ]]; then
    mv "$WEBMAIL_ROOT" "$WEBMAIL_ROOT.old"
  fi
  mv "$new" "$WEBMAIL_ROOT"
  rm -rf "$work"
  ok "Roundcube $rc_version installed"
else
  ok "Roundcube $rc_version is up to date"
fi

if [[ ! -f "$WEBMAIL_ROOT/config/config.inc.php" ]]; then
  sed -e "s#@DES_KEY@#$(random_password 24)#" -e "s#@DATA@#$WEBMAIL_DATA#g" \
    "$TELA_DIR/install/webmail/config.inc.php.in" >"$WEBMAIL_ROOT/config/config.inc.php"
fi

# Database: created from Roundcube's schema on first install, upgraded after.
db="$WEBMAIL_DATA/db/roundcube.db"
php_cli=""
for candidate in "$OLS_ROOT/lsphp${TELA_PHP/./}/bin/php" "$OLS_ROOT"/lsphp8*/bin/php; do
  if [[ -x "$candidate" ]]; then
    php_cli="$candidate"
    break
  fi
done
if [[ ! -f "$db" ]]; then
  run python3 -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.executescript(open(sys.argv[2]).read()); c.commit()' \
    "$db" "$WEBMAIL_ROOT/SQL/sqlite.initial.sql"
  chown "$WEBMAIL_USER:$WEBMAIL_USER" "$db"
  chmod 0600 "$db"
elif [[ -n "$installed_version" && "$installed_version" != "$rc_version" && -n "$php_cli" ]]; then
  run runuser -u "$WEBMAIL_USER" -- "$php_cli" "$WEBMAIL_ROOT/bin/updatedb.sh" \
    --package=roundcube --dir="$WEBMAIL_ROOT/SQL"
fi
rm -rf "$WEBMAIL_ROOT.old"

# Code is owned by root and only readable by the webmail PHP user; static files
# are served through Roundcube's static.php, so the web server needs no access.
chown -R root:"$WEBMAIL_USER" "$WEBMAIL_ROOT"
find "$WEBMAIL_ROOT" -type d -exec chmod 0750 {} +
find "$WEBMAIL_ROOT" -type f -exec chmod 0640 {} +
chmod 0755 "$WEBMAIL_ROOT" "$WEBMAIL_ROOT/public_html"
find "$WEBMAIL_ROOT/bin" -type f -name "*.sh" -exec chmod 0750 {} +

# Serve it on the mail host name.
run env PYTHONPATH="$TELA_DIR/src" "$TELA_VENV/bin/python" -m tela_agent.setup
ols_reload
ok "Webmail on https://$(python3 -c 'import json; print(json.load(open("/etc/tela/mail/settings.json"))["hostname"])' 2>/dev/null || echo "<mail host name>")/"
