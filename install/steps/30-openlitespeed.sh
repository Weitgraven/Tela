# shellcheck shell=bash
step "Installing OpenLiteSpeed and PHP"

if ! compgen -G "/etc/apt/sources.list.d/lst_debian_repo*" >/dev/null; then
  repo_script="$(mktemp)"
  run curl -fsSL https://repo.litespeed.sh -o "$repo_script"
  run bash "$repo_script"
  rm -f "$repo_script"
  run apt-get update -q
  ok "LiteSpeed package repository added"
fi

apt_install openlitespeed

# Install every supported PHP version so each site can pick its own. The default
# version (--php) is required; the others are installed when available.
# Extensions most PHP apps (WordPress, Laravel, ...) expect. Not every one exists
# for every PHP version, so a missing one is skipped.
php_extensions=(common mysql curl intl imagick opcache redis memcached imap ldap sqlite3 tidy)
installed_php=()
for version in $TELA_PHP_VERSIONS "$TELA_PHP"; do
  php_pkg="lsphp${version/./}"
  [[ " ${installed_php[*]} " == *" $version "* ]] && continue
  if ! apt-cache show "$php_pkg" >/dev/null 2>&1; then
    if [[ "$version" == "$TELA_PHP" ]]; then
      die "PHP $version ($php_pkg) is not available from the LiteSpeed repository"
    fi
    echo "skipped $php_pkg (not available)" >>"$LOG_FILE"
    continue
  fi
  wanted=("$php_pkg")
  for ext in "${php_extensions[@]}"; do
    pkg="${php_pkg}-${ext}"
    if is_installed "$pkg" || apt-cache show "$pkg" >/dev/null 2>&1; then
      wanted+=("$pkg")
    else
      echo "skipped $pkg (not available)" >>"$LOG_FILE"
    fi
  done
  apt_install "${wanted[@]}"
  installed_php+=("$version")
done
ok "OpenLiteSpeed with PHP ${installed_php[*]}"

admin_conf="$OLS_ROOT/admin/conf/admin_config.conf"

# One-time hardening of a fresh OpenLiteSpeed install. Tracked with a marker file
# rather than "was the package just installed", so a re-run after a failed
# install still applies it.
ols_marker="$OLS_ROOT/conf/.tela-initialized"
if [[ ! -f "$ols_marker" ]]; then
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
  touch "$ols_marker"
fi

# Point the server-wide PHP handler at the default version.
default_php="$OLS_ROOT/lsphp${TELA_PHP/./}/bin/lsphp"
if [[ -x "$default_php" ]]; then
  ln -sf "$default_php" "$OLS_ROOT/fcgi-bin/lsphp"
fi

# An earlier Tela installer replaced the lsws.service alias with a copy of
# lshttpd.service. Put the alias back so there is only one web server unit.
units=/etc/systemd/system
if [[ -f "$units/lshttpd.service" && -f "$units/lsws.service" && ! -L "$units/lsws.service" ]] \
  && cmp -s "$units/lshttpd.service" "$units/lsws.service"; then
  ln -sf "$units/lshttpd.service" "$units/lsws.service"
  run systemctl daemon-reload
  echo "restored lsws.service as an alias of lshttpd.service" >>"$LOG_FILE"
fi

enable_service lsws
run systemctl restart lsws
ok "Web server running"
