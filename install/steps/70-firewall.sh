# shellcheck shell=bash
step "Checking the firewall"

# Tela will manage its own firewall in a later release. For now it only opens
# the ports it needs if ufw is already active, and never enables a firewall
# itself (that could lock you out of SSH).
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
  for port in 80/tcp 443/tcp 443/udp "$TELA_PORT/tcp" "$TELA_PMA_PORT/tcp"; do
    run ufw allow "$port"
  done
  ok "Opened 80, 443, $TELA_PORT and $TELA_PMA_PORT in ufw"
  if [[ "${TELA_DNS_SERVER:-0}" == "1" ]]; then
    run ufw allow 53/tcp
    run ufw allow 53/udp
    ok "Opened 53 for DNS in ufw"
  fi
else
  info "No active firewall found. Make sure ports 80, 443, $TELA_PORT and $TELA_PMA_PORT are reachable."
fi
