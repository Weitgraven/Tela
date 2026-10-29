# shellcheck shell=bash
step "Checking this server"

# shellcheck source=/dev/null
. /etc/os-release
SUPPORTED_UBUNTU="24.04 26.04"
if [[ "${ID:-}" != "ubuntu" || " $SUPPORTED_UBUNTU " != *" ${VERSION_ID:-} "* ]]; then
  if [[ "${TELA_ALLOW_UNSUPPORTED:-}" == "1" ]]; then
    warn "${PRETTY_NAME:-unknown OS} is not supported; continuing because TELA_ALLOW_UNSUPPORTED=1"
  else
    die "Tela supports Ubuntu 24.04 and 26.04. This server runs ${PRETTY_NAME:-an unknown OS}."
  fi
fi
ok "${PRETTY_NAME}"

case "$(dpkg --print-architecture)" in
  amd64 | arm64) ok "Architecture $(dpkg --print-architecture)" ;;
  *) die "Unsupported CPU architecture: $(dpkg --print-architecture)" ;;
esac

if ! command -v systemctl >/dev/null || [[ ! -d /run/systemd/system ]]; then
  die "systemd is required. Containers without systemd are not supported."
fi

for other in /usr/local/CyberCP /usr/local/cpanel /usr/local/psa /usr/local/directadmin /usr/local/hestia; do
  if [[ -d "$other" ]]; then
    die "Another hosting panel is installed ($other). Use a fresh server."
  fi
done

mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if ((mem_mb < 1800)); then
  warn "Only ${mem_mb} MB of memory. 2 GB or more is recommended, especially for email."
else
  ok "${mem_mb} MB memory"
fi

disk_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
((disk_gb >= 10)) || die "At least 10 GB of free disk space is needed; ${disk_gb} GB is free."
ok "${disk_gb} GB free disk space"

if ! [[ "$TELA_PORT" =~ ^[0-9]+$ ]] || ((TELA_PORT < 1 || TELA_PORT > 65535)); then
  die "Invalid panel port: $TELA_PORT"
fi
[[ "$TELA_PHP" =~ ^8\.[0-9]$ ]] || die "Invalid PHP version: $TELA_PHP (use e.g. 8.3)"

# Ports must be free, unless something of ours already uses them (re-run).
for port in 80 443 "$TELA_PORT" "$TELA_PMA_PORT"; do
  owner=$(ss -Hltnp "sport = :$port" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2 || true)
  case "$owner" in
    "" | litespeed | lshttpd | openlitespeed | python3 | python) ;;
    *) die "Port $port is already used by '$owner'. Stop it first." ;;
  esac
done
ok "Ports 80, 443, $TELA_PORT and $TELA_PMA_PORT are available"

for port in 25 465 587 110 143 993 995; do
  owner=$(ss -Hltnp "sport = :$port" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2 || true)
  case "$owner" in
    "" | master | dovecot) ;;
    *) die "Mail port $port is already used by '$owner'. Remove that mail server first." ;;
  esac
done
ok "Mail ports are available"

if [[ "${TELA_DNS_SERVER:-0}" == "1" ]]; then
  owner=$(ss -Hlunp "sport = :53" 2>/dev/null | grep -v "127.0.0.5[34]" \
    | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2 || true)
  case "$owner" in
    "" | pdns_server | systemd-resolve) ok "Port 53 is available for the DNS server" ;;
    *) die "Port 53 is already used by '$owner'. Stop it first, or install without --with-dns-server." ;;
  esac
fi

if [[ -n "$TELA_HOSTNAME" ]]; then
  [[ "$TELA_HOSTNAME" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] \
    || die "Invalid hostname: $TELA_HOSTNAME"
  run hostnamectl set-hostname "$TELA_HOSTNAME"
  ok "Hostname set to $TELA_HOSTNAME"
fi
