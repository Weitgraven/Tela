# shellcheck shell=bash
step "Installing MariaDB"

first_install=0
is_installed mariadb-server || first_install=1

apt_install mariadb-server mariadb-client
run systemctl enable mariadb
run systemctl start mariadb

if ((first_install)); then
  # Same clean-up as mysql_secure_installation. root keeps unix_socket login,
  # so only the system root user (and tela-agent) can reach it.
  run mariadb --protocol=socket -e "
    DELETE FROM mysql.global_priv WHERE User='';
    DELETE FROM mysql.global_priv WHERE User='root' AND Host NOT IN ('localhost');
    DROP DATABASE IF EXISTS test;
    DELETE FROM mysql.db WHERE Db='test' OR Db='test\\_%';
    FLUSH PRIVILEGES;"
  ok "Removed anonymous users and the test database"
fi

conf=/etc/mysql/mariadb.conf.d/90-tela.cnf
if write_if_changed "$conf" <<'EOF'; then
# Managed by Tela. Local connections only.
[mysqld]
bind-address = 127.0.0.1
skip-name-resolve
EOF
  run systemctl restart mariadb
fi
ok "MariaDB running (local connections only)"
