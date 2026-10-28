<?php
/**
 * Tela single sign-on for phpMyAdmin.
 *
 * The Tela panel asks tela-agent for a sign-on link. The agent creates a
 * temporary MariaDB user that can only reach one website's databases and writes
 * its credentials to a one-time token file readable only by this PHP user.
 * This script reads and deletes that file, hands the credentials to phpMyAdmin
 * through its "signon" authentication, and redirects. The token is valid once,
 * for one minute.
 */

declare(strict_types=1);

const TELA_TOKEN_DIR = '/var/lib/tela-pma/tokens';
const TELA_SESSION = 'TelaPMASignon';

function tela_page(int $status, string $title, string $message): never
{
    http_response_code($status);
    header('Content-Type: text/html; charset=utf-8');
    header('Cache-Control: no-store');
    header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'");
    $t = htmlspecialchars($title, ENT_QUOTES);
    $m = htmlspecialchars($message, ENT_QUOTES);
    echo "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        . "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        . "<title>$t</title><style>body{font:16px/1.5 system-ui,sans-serif;margin:0;"
        . "display:grid;place-items:center;min-height:100vh;background:#edf0f2;color:#1c2340}"
        . "main{max-width:30rem;padding:2rem}h1{font-size:1.5rem;margin:0 0 .5rem}"
        . "p{margin:0;color:#5a6380}</style></head><body><main><h1>$t</h1><p>$m</p>"
        . "</main></body></html>";
    exit;
}

function tela_start_session(): void
{
    session_name(TELA_SESSION);
    session_set_cookie_params([
        'lifetime' => 0,
        'path' => '/',
        'secure' => true,
        'httponly' => true,
        'samesite' => 'Lax',
    ]);
    session_start();
}

if (isset($_GET['logged_out'])) {
    tela_start_session();
    $_SESSION = [];
    session_destroy();
    tela_page(200, 'Signed out of phpMyAdmin', 'You can close this tab. Open phpMyAdmin again from the Tela panel.');
}

$token = $_GET['token'] ?? '';
if (!is_string($token) || !preg_match('/^[A-Za-z0-9_-]{32,64}$/', $token)) {
    tela_page(403, 'Open phpMyAdmin from Tela', 'Go to Databases in the Tela panel and click phpMyAdmin.');
}

$file = TELA_TOKEN_DIR . '/' . $token . '.json';
$raw = @file_get_contents($file);
@unlink($file);
$data = is_string($raw) ? json_decode($raw, true) : null;

if (!is_array($data) || !isset($data['user'], $data['password'], $data['expires'])
    || !is_string($data['user']) || !is_string($data['password'])
    || (int) $data['expires'] < time()) {
    tela_page(403, 'This link has expired', 'Sign-on links work once, for one minute. Open phpMyAdmin again from the Tela panel.');
}

tela_start_session();
session_regenerate_id(true);
$_SESSION['PMA_single_signon_user'] = $data['user'];
$_SESSION['PMA_single_signon_password'] = $data['password'];
$_SESSION['PMA_single_signon_host'] = 'localhost';
$_SESSION['PMA_single_signon_HMAC_secret'] = bin2hex(random_bytes(32));
session_write_close();

$target = 'index.php';
if (isset($data['db']) && is_string($data['db']) && preg_match('/^[a-z][a-z0-9_]{0,63}$/', $data['db'])) {
    $target .= '?route=/database/structure&db=' . rawurlencode($data['db']);
}
header('Cache-Control: no-store');
header('Location: ' . $target, true, 303);
exit;
