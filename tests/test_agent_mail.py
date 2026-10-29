from __future__ import annotations

import pytest

from tela_agent import mailconf
from tela_agent.actions import dns, mail
from tela_agent.registry import ActionError
from tela_agent.sha512crypt import verify


@pytest.fixture
def mailhost(host):
    fake, paths = host
    mail.update_settings({"hostname": "mail.example.com"})
    return fake, paths


def users(fake) -> str:
    return fake.mail.dovecot_users.read_text()


def table(fake, name) -> str:
    return (fake.mail.postfix_dir / name).read_text()


def test_hostname_validation(host):
    with pytest.raises(ActionError, match="host name like"):
        mail.update_settings({"hostname": "example.com"})


def test_settings_configure_postfix(mailhost):
    fake, _ = mailhost
    assert fake.mail.postconf_calls[-1]["myhostname"] == "mail.example.com"
    assert fake.mail.postconf_calls[-1]["mynetworks"] == "127.0.0.0/8 [::1]/128"
    assert mail.status({})["hostname"] == "mail.example.com"


def test_domain_mailbox_alias(mailhost):
    fake, _ = mailhost
    info = mail.create_domain({"domain": "example.com", "dns": False})
    assert info["records"][0] == {"name": "@", "type": "MX", "content": "mail.example.com",
                                  "priority": 10}  # fmt: skip
    assert info["records"][2]["name"] == "tela._domainkey"
    assert info["records"][2]["content"].startswith("v=DKIM1; k=rsa; p=MII")

    box = mail.create_mailbox({"domain": "example.com", "local": "Info", "quota_mb": 500})
    assert box["address"] == "info@example.com" and len(box["password"]) == 20
    line = users(fake).splitlines()[0]
    assert line.startswith("info@example.com:{SHA512-CRYPT}$6$")
    assert line.endswith(":userdb_quota_storage_size=500M")
    assert verify(box["password"], line.split(":")[1].removeprefix("{SHA512-CRYPT}"))
    assert "info@example.com example.com/info/" in table(fake, "mailboxes")
    assert "example.com OK" in table(fake, "domains")

    mail.create_alias({"domain": "example.com", "local": "sales",
                       "destinations": ["info@example.com", "boss@gmail.com"]})  # fmt: skip
    aliases = table(fake, "aliases")
    assert "sales@example.com info@example.com,boss@gmail.com" in aliases
    assert "sales@example.com info@example.com" in table(fake, "senders")


def test_catchall_keeps_mailboxes(mailhost):
    fake, _ = mailhost
    mail.create_domain({"domain": "example.com", "dns": False})
    mail.create_mailbox({"domain": "example.com", "local": "info"})
    mail.create_alias({"domain": "example.com", "local": "*", "destinations": ["info@example.com"]})
    aliases = table(fake, "aliases").splitlines()
    assert "info@example.com info@example.com" in aliases
    assert aliases[-1] == "@example.com info@example.com"
    with pytest.raises(ActionError, match="already has a catch-all"):
        mail.create_alias({"domain": "example.com", "local": "*", "destinations": ["x@y.nl"]})


@pytest.mark.parametrize("local", ["", ".x", "x.", "a..b", "a b", "a@b", "x" * 65, "ü"])
def test_invalid_local_parts(mailhost, local):
    mail.create_domain({"domain": "example.com", "dns": False})
    with pytest.raises(ActionError):
        mail.create_mailbox({"domain": "example.com", "local": local})


def test_password_rules(mailhost):
    mail.create_domain({"domain": "example.com", "dns": False})
    with pytest.raises(ActionError, match="10 to 128"):
        mail.create_mailbox({"domain": "example.com", "local": "a", "password": "short"})
    with pytest.raises(ActionError, match="':'"):
        mail.create_mailbox({"domain": "example.com", "local": "a", "password": "has:colon-inside"})
    box = mail.create_mailbox(
        {"domain": "example.com", "local": "a", "password": "my-own-password"}
    )
    assert "password" not in box


def test_names_are_unique_across_mailboxes_and_aliases(mailhost):
    mail.create_domain({"domain": "example.com", "dns": False})
    mail.create_mailbox({"domain": "example.com", "local": "info"})
    with pytest.raises(ActionError, match="already exists"):
        mail.create_alias({"domain": "example.com", "local": "info", "destinations": ["x@y.nl"]})


def test_disable_and_password_change(mailhost):
    fake, _ = mailhost
    mail.create_domain({"domain": "example.com", "dns": False})
    box = mail.create_mailbox({"domain": "example.com", "local": "info"})
    mail.update_mailbox({"domain": "example.com", "local": "info", "enabled": False})
    hashed = users(fake).split(":")[1].removeprefix("{SHA512-CRYPT}")
    assert not verify(box["password"], hashed)
    assert "info@example.com" in table(fake, "mailboxes")  # still receives mail
    result = mail.update_mailbox({"domain": "example.com", "local": "info", "enabled": True,
                                  "password": ""})  # fmt: skip
    hashed = users(fake).split(":")[1].removeprefix("{SHA512-CRYPT}")
    assert verify(result["password"], hashed)


def test_users_file_mtime_always_moves(mailhost):
    fake, _ = mailhost
    mail.create_domain({"domain": "example.com", "dns": False})
    mail.create_mailbox({"domain": "example.com", "local": "info"})
    before = fake.mail.dovecot_users.stat().st_mtime
    mail.update_mailbox({"domain": "example.com", "local": "info", "password": ""})
    assert fake.mail.dovecot_users.stat().st_mtime >= int(before) + 1


def test_failed_reload_restores_state(mailhost):
    fake, _ = mailhost
    mail.create_domain({"domain": "example.com", "dns": False})
    fake.mail.fail_reload = True
    with pytest.raises(ActionError, match="Postfix"):
        mail.create_mailbox({"domain": "example.com", "local": "info"})
    fake.mail.fail_reload = False
    assert mail.get_domain({"domain": "example.com"})["mailboxes"] == []


def test_delete_domain(mailhost):
    fake, _ = mailhost
    mail.create_domain({"domain": "example.com", "dns": False})
    mail.create_mailbox({"domain": "example.com", "local": "info"})
    (fake.mail.vmail_root / "example.com" / "info").mkdir(parents=True)
    mail.delete_domain({"domain": "example.com"})
    assert "example.com" not in table(fake, "domains")
    assert not (fake.mail.dkim_dir / "example.com.tela.key").exists()
    assert not (fake.mail.vmail_root / "example.com").exists()


def test_dns_records_added_to_zone(mailhost):
    dns.update_settings({"public_ipv4": "203.0.113.5", "nameservers": ["ns1.host.nl"]})
    dns.create_zone({"zone": "example.com", "provider": "local"})
    info = mail.create_domain({"domain": "example.com"})
    assert sorted(info["dns"]["added"]) == [
        "MX example.com", "TXT _dmarc.example.com", "TXT example.com",
        "TXT tela._domainkey.example.com",
    ]  # fmt: skip
    records = dns.list_records({"zone": "example.com"})["records"]
    spf = next(r for r in records if r["content"].startswith("v=spf1"))
    assert spf["content"] == "v=spf1 ip4:203.0.113.5 mx ~all"
    assert any(r["name"] == "mail" and r["type"] == "A" for r in records)
    again = mail.domain_dns({"domain": "example.com"})
    assert again["added"] == [] and len(again["skipped"]) == 4


def test_dovecot_conf_versions():
    from pathlib import Path

    for version, marker in (((2, 3), "mail_location = maildir"), ((2, 4), "mail_driver = maildir")):
        text = mailconf.dovecot_conf(version, users_file=Path("/u"), cert=Path("/c"),
                                     key=Path("/k"), uid=5000, ipv6=False)  # fmt: skip
        assert marker in text
        assert "listen = *\n" in text
    assert "dovecot_config_version = 2.4.0" in mailconf.dovecot_conf(
        (2, 4), users_file=Path("/u"), cert=Path("/c"), key=Path("/k"), uid=5000
    )


def test_setup_server(host, tmp_path):
    import dataclasses

    fake, paths = host
    paths = dataclasses.replace(paths, panel_cert=tmp_path / "panel.crt",
                                panel_key=tmp_path / "panel.key")  # fmt: skip
    message = mail.setup_server(paths)
    assert "Dovecot 2.4" in message
    assert "dovecot_config_version" in fake.mail.dovecot_conf.read_text()
    assert "try_fallback = true;" in (fake.mail.rspamd_local / "dkim_signing.conf").read_text()
    assert "submission" in fake.mail.reloads and "doveconf" in fake.mail.reloads


def test_mail_vhost(mailhost):
    from tela_agent import ols
    from tela_agent.actions import websites

    fake, paths = mailhost
    files = ols.desired_files(websites.load_sites(paths), paths)
    vhconf = files[paths.vhost_conf_root / "TelaMail" / "vhconf.conf"]
    assert "vhDomain                mail.example.com" in vhconf
    assert "$VH_ROOT/public\n" in vhconf and "extprocessor" not in vhconf
    assert f"{paths.mailhost_root}/.acme/.well-known/acme-challenge/" in vhconf
    assert (
        "map                     TelaMail mail.example.com"
        in files[paths.tela_conf_dir / "zz-listeners.conf"]
    )

    # With webmail installed and a certificate: PHP as tela-webmail, HTTPS redirect.
    (paths.webmail_root / "public_html").mkdir(parents=True)
    (paths.webmail_root / "index.php").write_text("<?php")
    settings_file = paths.mail_dir / "settings.json"
    import json

    conf = json.loads(settings_file.read_text())
    conf.update(ssl_cert="/le/fullchain.pem", ssl_key="/le/privkey.pem")
    settings_file.write_text(json.dumps(conf))
    files = ols.desired_files(websites.load_sites(paths), paths)
    vhconf = files[paths.vhost_conf_root / "TelaMail" / "vhconf.conf"]
    assert "$VH_ROOT/public_html" in vhconf
    assert "extUser                 tela-webmail" in vhconf
    assert "https://%{HTTP_HOST}" in vhconf
    assert "certFile                /le/fullchain.pem" in vhconf
    assert (
        f"vhRoot                  {paths.webmail_root}/"
        in files[paths.tela_conf_dir / "mailhost.conf"]
    )


def test_website_cant_take_mail_hostname(mailhost):
    from tela_agent.actions import websites

    with pytest.raises(ActionError, match="mail server's host name"):
        websites.create_site({"domain": "mail.example.com", "php_version": "8.3"})
