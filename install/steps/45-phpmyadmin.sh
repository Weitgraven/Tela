# shellcheck shell=bash
step "Installing phpMyAdmin"

PMA_ROOT="/opt/tela-phpmyadmin"
PMA_USER="tela-pma"
PMA_TOKENS="/var/lib/tela-pma/tokens"

if ! id "$PMA_USER" >/dev/null 2>&1; then
  run useradd --system --home-dir /var/lib/tela-pma --no-create-home \
    --shell /usr/sbin/nologin "$PMA_USER"
fi
install -d -m 0750 -o "$PMA_USER" -g "$PMA_USER" /var/lib/tela-pma
install -d -m 0700 -o "$PMA_USER" -g "$PMA_USER" "$PMA_TOKENS" /var/lib/tela-pma/sessions

# Latest phpMyAdmin release, checked against its published SHA-256.
pma_version="$(curl -fsSL https://www.phpmyadmin.net/home_page/version.txt || true)"
pma_version="${pma_version%%$'\n'*}"
[[ "$pma_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "could not determine the latest phpMyAdmin version"

installed_version="$(cat "$PMA_ROOT/.tela-version" 2>/dev/null || true)"
if [[ "$installed_version" != "$pma_version" ]]; then
  work="$(mktemp -d)"
  archive="phpMyAdmin-${pma_version}-english.tar.xz"
  base_url="https://files.phpmyadmin.net/phpMyAdmin/${pma_version}"
  run curl -fsSL "$base_url/$archive" -o "$work/$archive"
  run curl -fsSL "$base_url/$archive.sha256" -o "$work/$archive.sha256"
  expected="$(awk '{print $1}' "$work/$archive.sha256")"
  actual="$(sha256sum "$work/$archive" | awk '{print $1}')"
  [[ -n "$expected" && "$expected" == "$actual" ]] || die "phpMyAdmin download failed its checksum"

  run tar -xJf "$work/$archive" -C "$work"
  new="$work/phpMyAdmin-${pma_version}-english"
  # Not needed, and safer gone.
  rm -rf "$new/setup" "$new/examples" "$new/test" "$new/doc/html/_sources"
  # Keep the existing configuration (and its secret) across upgrades.
  if [[ -f "$PMA_ROOT/config.inc.php" ]]; then
    cp -p "$PMA_ROOT/config.inc.php" "$new/config.inc.php"
  fi
  echo "$pma_version" >"$new/.tela-version"
  rm -rf "$PMA_ROOT.old"
  if [[ -d "$PMA_ROOT" ]]; then
    mv "$PMA_ROOT" "$PMA_ROOT.old"
  fi
  mv "$new" "$PMA_ROOT"
  rm -rf "$PMA_ROOT.old" "$work"
  ok "phpMyAdmin $pma_version installed"
else
  ok "phpMyAdmin $pma_version is up to date"
fi

if [[ ! -f "$PMA_ROOT/config.inc.php" ]]; then
  secret="$(openssl rand -hex 32)"
  sed -e "s#@BLOWFISH_SECRET@#$secret#" -e "s#@PMA_ROOT@#$PMA_ROOT#" \
    "$TELA_DIR/install/phpmyadmin/config.inc.php.in" >"$PMA_ROOT/config.inc.php"
fi
install -m 0640 "$TELA_DIR/install/phpmyadmin/tela-signon.php" "$PMA_ROOT/tela-signon.php"
install -d -m 0700 -o "$PMA_USER" -g "$PMA_USER" "$PMA_ROOT/tmp"

# Code is owned by root and only readable by the phpMyAdmin PHP user.
chown -R root:"$PMA_USER" "$PMA_ROOT"
chown -R "$PMA_USER":"$PMA_USER" "$PMA_ROOT/tmp"
find "$PMA_ROOT" -path "$PMA_ROOT/tmp" -prune -o -type d -exec chmod 0750 {} +
find "$PMA_ROOT" -path "$PMA_ROOT/tmp" -prune -o -type f -exec chmod 0640 {} +
# The web server (nobody:nogroup) serves static files (CSS, JS, images) itself.
chmod 0755 "$PMA_ROOT"
find "$PMA_ROOT" -path "$PMA_ROOT/tmp" -prune -o -type d -exec chmod o+x {} +
find "$PMA_ROOT/themes" "$PMA_ROOT/js" -type f -exec chmod o+r {} + 2>/dev/null || true
find "$PMA_ROOT" -maxdepth 1 -type f \( -name "*.ico" -o -name "*.png" \) -exec chmod o+r {} +
chmod 0640 "$PMA_ROOT/config.inc.php"
ok "phpMyAdmin sign-in only through the Tela panel (port $TELA_PMA_PORT)"
