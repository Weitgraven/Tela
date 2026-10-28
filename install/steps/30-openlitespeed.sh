# shellcheck shell=bash
step "Installing OpenLiteSpeed and PHP $TELA_PHP"

php_pkg="lsphp${TELA_PHP/./}"

if ! compgen -G "/etc/apt/sources.list.d/lst_debian_repo*" >/dev/null; then
  repo_script="$(mktemp)"
  run curl -fsSL https://repo.litespeed.sh -o "$repo_script"
  run bash "$repo_script"
  rm -f "$repo_script"
  run apt-get update -q
  ok "LiteSpeed package repository added"
fi

first_install=0
is_installed openlitespeed || first_install=1

apt_install openlitespeed "$php_pkg" "${php_pkg}-common" "${php_pkg}-mysql"

# Extensions most PHP apps (WordPress, Laravel, ...) expect. Not every one exists
# for every PHP version, so a missing one is a warning, not a failure.
for ext in curl intl imagick opcache redis memcached imap ldap sqlite3 tidy; do
  pkg="${php_pkg}-${ext}"
  if is_installed "$pkg"; then
    continue
  fi
  if apt-cache show "$pkg" >/dev/null 2>&1; then
    apt_install "$pkg"
  else
    echo "skipped $pkg (not available)" >>"$LOG_FILE"
  fi
done
ok "OpenLiteSpeed with $php_pkg installed"

httpd_conf="$OLS_ROOT/conf/httpd_config.conf"
admin_conf="$OLS_ROOT/admin/conf/admin_config.conf"

if ((first_install)); then
  # Serve on port 80 instead of the demo port 8088.
  sed -i -E 's/^(\s*address\s+)\*:8088\s*$/\1*:80/' "$httpd_conf"

  # The OpenLiteSpeed WebAdmin console is only reachable from the server itself
  # (use an SSH tunnel). Tela manages the web server; you shouldn't need it.
  sed -i -E 's/^(\s*address\s+)\*:7080\s*$/\1127.0.0.1:7080/' "$admin_conf"

  ols_pass="$(random_password 24)"
  hash="$("$OLS_ROOT/admin/fcgi-bin/admin_php" -q "$OLS_ROOT/admin/misc/htpasswd.php" "$ols_pass")"
  echo "admin:$hash" >"$OLS_ROOT/admin/conf/htpasswd"
  chown lsadm:lsadm "$OLS_ROOT/admin/conf/htpasswd" 2>/dev/null || true
  chmod 600 "$OLS_ROOT/admin/conf/htpasswd"
  save_credential "OpenLiteSpeed WebAdmin" "admin / $ols_pass (https://127.0.0.1:7080)"
  ok "WebAdmin console limited to localhost"
fi

# Point the default PHP handler at the chosen version.
if [[ -x "$OLS_ROOT/$php_pkg/bin/lsphp" ]]; then
  ln -sf "$OLS_ROOT/$php_pkg/bin/lsphp" "$OLS_ROOT/fcgi-bin/lsphp"
fi

run systemctl enable lsws
run systemctl restart lsws
ok "Web server running"
