# shellcheck shell=bash
step "Installing WP-CLI"

# The official stable build, checked against its published SHA-512.
WP_CLI="/usr/local/lib/tela/wp-cli.phar"
base="https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar"
work="$(mktemp -d)"
run curl -fsSL "$base/wp-cli.phar" -o "$work/wp-cli.phar"
run curl -fsSL "$base/wp-cli.phar.sha512" -o "$work/wp-cli.phar.sha512"
expected="$(tr -d '[:space:]' <"$work/wp-cli.phar.sha512")"
actual="$(sha512sum "$work/wp-cli.phar" | awk '{print $1}')"
[[ -n "$expected" && "$expected" == "$actual" ]] || die "WP-CLI download failed its checksum"
install -d -m 0755 /usr/local/lib/tela
install -m 0644 "$work/wp-cli.phar" "$WP_CLI"
rm -rf "$work"

# `wp` for administrators, with the default PHP version.
php_cli="$OLS_ROOT/lsphp${TELA_PHP/./}/bin/php"
cat >/usr/local/bin/wp <<EOF2
#!/bin/sh
# Installed by Tela. Run it as the website's user, e.g.:
#   sudo -u <site user> wp --path=/home/<domain>/public_html plugin list
exec "$php_cli" -d memory_limit=512M "$WP_CLI" "\$@"
EOF2
chmod 0755 /usr/local/bin/wp

if [[ -x "$php_cli" ]]; then
  ok "WP-CLI $("$php_cli" "$WP_CLI" --version --allow-root 2>/dev/null | awk '{print $2}') installed"
else
  warn "No command-line PHP at $php_cli; WordPress tools won't work until it's there."
fi
install -d -m 0700 -o root -g root "$TELA_ETC/wordpress"
