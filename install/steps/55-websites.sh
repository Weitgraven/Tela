# shellcheck shell=bash
step "Setting up websites and SSL"

apt_install certbot
enable_service certbot.timer
run systemctl start certbot.timer
ok "Let's Encrypt client installed; certificates renew automatically"

# Renewed certificates only take effect after the web server reloads them.
hook_dir=/etc/letsencrypt/renewal-hooks/deploy
install -d -m 0755 "$hook_dir"
write_if_changed "$hook_dir/tela-reload-lsws.sh" <<EOF || true
#!/bin/sh
# Installed by Tela: load renewed certificates.
exec $OLS_ROOT/bin/lswsctrl restart
EOF
chmod 0755 "$hook_dir/tela-reload-lsws.sh"

# Certificate served to visitors of a site that has no certificate of its own yet.
default_key="$OLS_ROOT/conf/tela-default.key"
default_cert="$OLS_ROOT/conf/tela-default.crt"
if [[ ! -f "$default_key" || ! -f "$default_cert" ]]; then
  run openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
    -keyout "$default_key" -out "$default_cert" -subj "/CN=$(hostname -f 2>/dev/null || hostname)"
  chmod 0600 "$default_key"
  ok "Created the default certificate for HTTPS"
fi

install -d -m 0700 -o root -g root "$TELA_ETC/sites"

# Hand the web server config over to Tela: replaces the demo listener and
# virtual host with Tela's, and rebuilds the config of existing sites.
run env PYTHONPATH="$TELA_DIR/src" "$TELA_VENV/bin/python" -m tela_agent.setup
run systemctl restart lsws
ok "Web server now serves Tela websites on ports 80 and 443"
