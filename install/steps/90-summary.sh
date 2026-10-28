# shellcheck shell=bash
ip="$(public_ip)"
printf '\n%s  Tela is installed.%s\n\n' "$C_BOLD$C_GREEN" "$C_RESET"
printf '  Panel     https://%s:%s\n' "${ip:-YOUR-SERVER-IP}" "$TELA_PORT"
if [[ -n "${TELA_NEW_ADMIN_PASSWORD:-}" ]]; then
  printf '  Username  admin\n'
  printf '  Password  %s\n' "$TELA_NEW_ADMIN_PASSWORD"
else
  printf '  Sign in with your existing account. Forgot it? Run: sudo tela user passwd admin\n'
fi
printf '\n  %sYour browser will warn about the certificate the first time; that is expected.\n' "$C_DIM"
printf '  Credentials are saved in %s (root only).%s\n\n' "$TELA_CREDENTIALS" "$C_RESET"
