#!/usr/bin/env bash
#
# Tela installer.
#
#   curl -sSL https://raw.githubusercontent.com/Weitgraven/Tela/main/install.sh | sudo bash
#
# Safe to run again: it updates the code in /opt/tela and re-applies every
# step without touching existing sites, databases, users or credentials.
#
set -Eeuo pipefail

TELA_REPO="${TELA_REPO:-https://github.com/Weitgraven/Tela.git}"
TELA_BRANCH="${TELA_BRANCH:-main}"
TELA_DIR="${TELA_DIR:-/opt/tela}"

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --hostname NAME     Set the server hostname (e.g. server1.example.com)
  --port PORT         Panel port (default 8090)
  --php VERSION       Default PHP version for new sites (default 8.3)
  --branch NAME       Git branch to install from (default main)
  -h, --help          Show this help

Environment:
  TELA_REPO, TELA_BRANCH, TELA_DIR    Where to get Tela from and install it to
  TELA_ALLOW_UNSUPPORTED=1            Try to install on an unsupported OS
EOF
}

TELA_HOSTNAME=""
TELA_PORT="8090"
TELA_PHP="8.3"
ARGS=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) TELA_HOSTNAME="${2:?--hostname needs a value}"; shift 2 ;;
    --port) TELA_PORT="${2:?--port needs a value}"; shift 2 ;;
    --php) TELA_PHP="${2:?--php needs a value}"; shift 2 ;;
    --branch) TELA_BRANCH="${2:?--branch needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
export TELA_REPO TELA_BRANCH TELA_DIR TELA_HOSTNAME TELA_PORT TELA_PHP

if [[ $EUID -ne 0 ]]; then
  echo "Run the installer as root (for example with sudo)." >&2
  exit 1
fi

# --- Bootstrap ---------------------------------------------------------------
# When piped from curl there is no checkout next to this script yet: fetch the
# code into $TELA_DIR and hand over to the copy of this script in there.
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ "${TELA_FROM_CHECKOUT:-}" == "1" ]]; then
  # Developer install straight from a local checkout, wherever it lives.
  [[ -f "$SCRIPT_DIR/install/lib.sh" ]] || { echo "Not a Tela checkout: $SCRIPT_DIR" >&2; exit 1; }
  TELA_DIR="$SCRIPT_DIR"
elif [[ "${TELA_BOOTSTRAPPED:-}" != "1" ]]; then
  echo "==> Fetching Tela ($TELA_BRANCH) into $TELA_DIR"
  export DEBIAN_FRONTEND=noninteractive
  if ! command -v git >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null
  fi
  if [[ -d "$TELA_DIR/.git" ]]; then
    git -C "$TELA_DIR" fetch --quiet origin "$TELA_BRANCH"
    git -C "$TELA_DIR" checkout --quiet -B "$TELA_BRANCH" "origin/$TELA_BRANCH"
    git -C "$TELA_DIR" reset --quiet --hard "origin/$TELA_BRANCH"
  else
    git clone --quiet --branch "$TELA_BRANCH" "$TELA_REPO" "$TELA_DIR"
  fi
  export TELA_BOOTSTRAPPED=1
  exec bash "$TELA_DIR/install.sh" "${ARGS[@]}"
fi
export TELA_DIR

# --- Install -----------------------------------------------------------------
# shellcheck source=install/lib.sh
source "$TELA_DIR/install/lib.sh"

LOG_FILE="/var/log/tela-install.log"
: >"$LOG_FILE"
chmod 600 "$LOG_FILE"
export LOG_FILE

banner
for step in "$TELA_DIR"/install/steps/*.sh; do
  # shellcheck source=/dev/null
  source "$step"
done
