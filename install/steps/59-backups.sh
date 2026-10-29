# shellcheck shell=bash
step "Setting up backups"

apt_install restic rsync openssh-client
install -d -m 0700 -o root -g root "$TELA_ETC/backups" /var/lib/tela-backup
ok "restic $(restic version 2>/dev/null | awk '{print $2}') installed"
