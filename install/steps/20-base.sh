# shellcheck shell=bash
step "Installing base packages"

run apt-get update -q
apt_install ca-certificates curl wget gnupg git openssl iproute2 \
  python3 python3-venv python3-pip tzdata cron logrotate
ok "Base packages installed"
