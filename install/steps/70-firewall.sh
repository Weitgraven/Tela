# shellcheck shell=bash
step "Checking the firewall"

# Tela will manage its own firewall in a later release. For now it only opens
# the ports it needs if ufw is already active, and never enables a firewall
# itself (that could lock you out of SSH).
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
  for port in 80/tcp 443/tcp 443/udp "$TELA_PORT/tcp"; do
    run ufw allow "$port"
  done
  ok "Opened 80, 443 and $TELA_PORT in ufw"
else
  info "No active firewall found. Make sure ports 80, 443 and $TELA_PORT are reachable."
fi
