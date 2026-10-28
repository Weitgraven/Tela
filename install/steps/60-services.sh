# shellcheck shell=bash
step "Starting Tela services"

changed=0
for unit in tela-agent.service tela-panel.service; do
  if sed "s#@TELA_DIR@#$TELA_DIR#g" "$TELA_DIR/systemd/$unit" \
    | write_if_changed "/etc/systemd/system/$unit"; then
    changed=1
  fi
done
if ((changed)); then
  run systemctl daemon-reload
fi

enable_service tela-agent
enable_service tela-panel
# Restart so an upgrade picks up new code.
run systemctl restart tela-agent
run systemctl restart tela-panel

for _ in $(seq 1 30); do
  if curl -fsk "https://127.0.0.1:$TELA_PORT/api/health" >/dev/null 2>&1; then
    ok "Panel is up"
    break
  fi
  sleep 1
done
curl -fsk "https://127.0.0.1:$TELA_PORT/api/health" >/dev/null 2>&1 \
  || die "The panel did not start. Check: journalctl -u tela-panel -n 50"
