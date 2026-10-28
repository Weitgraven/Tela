# shellcheck shell=bash
step "Installing the Tela panel"

if ! id "$TELA_USER" >/dev/null 2>&1; then
  run useradd --system --home-dir "$TELA_DATA" --no-create-home --shell /usr/sbin/nologin "$TELA_USER"
  ok "Created system user '$TELA_USER'"
fi

install -d -m 0750 -o root -g "$TELA_USER" "$TELA_ETC" "$TELA_ETC/ssl"
install -d -m 0750 -o "$TELA_USER" -g "$TELA_USER" "$TELA_DATA"

# Python environment
if [[ ! -x "$TELA_VENV/bin/python" ]]; then
  run python3 -m venv "$TELA_VENV"
fi
run "$TELA_VENV/bin/pip" install --quiet --upgrade pip
run "$TELA_VENV/bin/pip" install --quiet -r "$TELA_DIR/requirements.txt"
ok "Python environment ready"

# Panel TLS certificate (self-signed until a hostname certificate is issued)
cert="$TELA_ETC/ssl/panel.crt"
key="$TELA_ETC/ssl/panel.key"
if [[ ! -f "$cert" || ! -f "$key" ]]; then
  host="$(hostname -f 2>/dev/null || hostname)"
  ip="$(public_ip)"
  san="DNS:$host,DNS:localhost,IP:127.0.0.1"
  if [[ -n "$ip" ]]; then
    san="$san,IP:$ip"
  fi
  run openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
    -keyout "$key" -out "$cert" -subj "/CN=$host" -addext "subjectAltName=$san"
  chown root:"$TELA_USER" "$key" "$cert"
  chmod 0640 "$key"
  chmod 0644 "$cert"
  ok "Created a self-signed certificate for the panel"
fi

# Configuration: written once, then left alone so local edits survive upgrades.
conf="$TELA_ETC/tela.conf"
if [[ ! -f "$conf" ]]; then
  cat >"$conf" <<EOF
# Tela panel configuration
[panel]
host = "0.0.0.0"
port = $TELA_PORT
tls_cert = "$cert"
tls_key = "$key"

[paths]
data_dir = "$TELA_DATA"
agent_socket = "/run/tela/agent.sock"
web_dir = "$TELA_DIR/web"

[security]
session_hours = 12
idle_minutes = 120

[defaults]
php_version = "$TELA_PHP"

[phpmyadmin]
port = $TELA_PMA_PORT
EOF
  chown root:"$TELA_USER" "$conf"
  chmod 0640 "$conf"
  ok "Wrote $conf"
fi

# `tela` command for administrators. It always runs as the panel user, so files
# it creates stay readable by the panel.
cat >/usr/local/bin/tela <<EOF
#!/bin/sh
# Installed by Tela.
if [ "\$(id -u)" -ne 0 ]; then
  echo "Run tela as root (sudo tela ...)." >&2
  exit 1
fi
cd /
umask 027
exec runuser -u $TELA_USER -- env PYTHONPATH="$TELA_DIR/src" TELA_CONFIG="$conf" \\
  "$TELA_VENV/bin/python" -m tela "\$@"
EOF
chmod 0755 /usr/local/bin/tela

run tela migrate

if [[ "$(tela user list | wc -l)" -eq 0 ]]; then
  admin_pass="$(random_password 20)"
  printf '%s\n' "$admin_pass" | run tela user add admin --role admin --password-stdin
  save_credential "Tela admin" "admin / $admin_pass"
  TELA_NEW_ADMIN_PASSWORD="$admin_pass"
  export TELA_NEW_ADMIN_PASSWORD
  ok "Created the admin account"
fi
