# Shared helpers for the installer. Sourced by install.sh.
# shellcheck shell=bash

export DEBIAN_FRONTEND=noninteractive

if [[ -t 1 ]]; then
  C_BOLD=$'\e[1m'; C_DIM=$'\e[2m'; C_RED=$'\e[31m'; C_GREEN=$'\e[32m'; C_YELLOW=$'\e[33m'
  C_BLUE=$'\e[34m'; C_RESET=$'\e[0m'
else
  C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_RESET=""
fi

# Used by the step scripts.
# shellcheck disable=SC2034
{
  TELA_ETC="/etc/tela"
  TELA_DATA="/var/lib/tela"
  TELA_USER="tela"
  TELA_VENV="$TELA_DIR/venv"
  TELA_CREDENTIALS="/root/tela-credentials.txt"
  OLS_ROOT="/usr/local/lsws"
}

banner() {
  printf '\n%s  Tela installer%s\n' "$C_BOLD" "$C_RESET"
  printf '%s  Free, open-source hosting panel. Log: %s%s\n\n' "$C_DIM" "$LOG_FILE" "$C_RESET"
}

step() { printf '%s==>%s %s%s%s\n' "$C_BLUE" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
info() { printf '    %s\n' "$*"; }
ok() { printf '    %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die() {
  printf '\n%sError:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
  printf 'Details are in %s\n' "$LOG_FILE" >&2
  exit 1
}

# Run a command with its output sent to the log; show the tail on failure.
run() {
  echo "+ $*" >>"$LOG_FILE"
  if ! "$@" >>"$LOG_FILE" 2>&1; then
    printf '%s    Command failed: %s%s\n' "$C_RED" "$*" "$C_RESET" >&2
    tail -n 15 "$LOG_FILE" | sed 's/^/    | /' >&2
    die "installation stopped"
  fi
}

apt_install() { run apt-get install -y -q --no-install-recommends "$@"; }

is_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

random_password() { openssl rand -base64 64 | tr -dc 'A-Za-z0-9' | cut -c1-"${1:-24}"; }

# Write a file only if its content changed. Returns 0 if it was written.
write_if_changed() {
  local dest="$1" tmp
  tmp="$(mktemp)"
  cat >"$tmp"
  if [[ -f "$dest" ]] && cmp -s "$tmp" "$dest"; then
    rm -f "$tmp"
    return 1
  fi
  install -m 0644 "$tmp" "$dest"
  rm -f "$tmp"
  return 0
}

save_credential() {
  local label="$1" value="$2"
  touch "$TELA_CREDENTIALS"
  chmod 600 "$TELA_CREDENTIALS"
  printf '%-28s %s\n' "$label" "$value" >>"$TELA_CREDENTIALS"
}

public_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null \
      | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)"
  fi
  if [[ -z "$ip" ]]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  echo "$ip"
}

trap 'die "unexpected error on line $LINENO"' ERR
