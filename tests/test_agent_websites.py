from __future__ import annotations

import json

import pytest

from tela_agent import ols
from tela_agent.actions import websites
from tela_agent.registry import ActionError


def create(domain="example.com", **extra):
    return websites.create_site({"domain": domain, "php_version": "8.3", **extra})


def test_create_site_writes_config(host):
    fake, paths = host
    site = create()
    assert site["domain"] == "example.com"
    assert site["aliases"] == ["www.example.com"]
    assert site["user"] == "example"
    assert fake.reloads == 1

    httpd = paths.httpd_config.read_text()
    assert "listener Default" not in httpd
    assert "virtualhost Example" not in httpd
    assert "module cache" in httpd  # everything else is untouched
    assert f"include {paths.tela_conf_dir}/*.conf" in httpd

    vhost = (paths.tela_conf_dir / "vhost-example.com.conf").read_text()
    assert "virtualhost example.com {" in vhost
    assert str(paths.sites_root / "example.com") in vhost

    listeners = (paths.tela_conf_dir / "zz-listeners.conf").read_text()
    assert "map                     example.com example.com, www.example.com" in listeners
    assert "*:80" in listeners and "*:443" in listeners

    vhconf = (paths.vhost_conf_root / "example.com" / "vhconf.conf").read_text()
    assert "lsphp83/bin/lsphp" in vhconf
    assert "extUser                 example" in vhconf
    assert "vhssl" not in vhconf

    home = paths.sites_root / "example.com"
    assert (home / "public_html" / "index.html").exists()
    assert (home / ".acme" / ".well-known" / "acme-challenge").is_dir()
    state = json.loads((paths.state_dir / "example.com.json").read_text())
    assert state["php_version"] == "8.3"


def test_include_is_added_once(host):
    _, paths = host
    create("a.com")
    create("b.com")
    assert paths.httpd_config.read_text().count("include ") == 1


def test_www_input_is_normalized(host):
    site = create("WWW.Example.COM")
    assert site["domain"] == "example.com"


def test_idn_domain(host):
    site = create("bücher.de")
    assert site["domain"] == "xn--bcher-kva.de"


@pytest.mark.parametrize(
    "bad", ["", "localhost", "exa mple.com", "-a.com", "a..com", "../etc", "a.com;rm", "1.2.3.4"]
)
def test_invalid_domains(host, bad):
    with pytest.raises(ActionError):
        create(bad)


def test_duplicate_and_alias_conflicts(host):
    create("example.com")
    with pytest.raises(ActionError, match="already exists"):
        create("example.com")
    with pytest.raises(ActionError, match="already used"):
        create("other.com", aliases=["www.example.com"])


def test_unique_usernames(host):
    a = create("example.com")
    b = create("example.net")
    assert a["user"] == "example"
    assert b["user"] == "example2"


def test_reserved_username_avoided(host):
    assert create("admin.com")["user"] == "admin2"


def test_php_version_must_be_installed(host):
    with pytest.raises(ActionError, match="not installed"):
        websites.create_site({"domain": "example.com", "php_version": "8.1"})


def test_failed_reload_rolls_back(host):
    fake, paths = host
    create("first.com")
    before = paths.httpd_config.read_text()
    listeners_before = (paths.tela_conf_dir / "zz-listeners.conf").read_text()
    fake.fail_reload = True
    with pytest.raises(ActionError):
        create("second.com")
    assert paths.httpd_config.read_text() == before
    assert (paths.tela_conf_dir / "zz-listeners.conf").read_text() == listeners_before
    assert not (paths.tela_conf_dir / "vhost-second.com.conf").exists()
    assert not (paths.state_dir / "second.com.json").exists()
    assert not (paths.sites_root / "second.com").exists()
    assert "second" not in fake.users


def test_update_php_and_aliases(host):
    _, paths = host
    create()
    site = websites.update_site(
        {"domain": "example.com", "php_version": "8.2", "aliases": ["shop.example.com"]}
    )
    assert site["php_version"] == "8.2"
    assert site["aliases"] == ["shop.example.com"]
    vhconf = (paths.vhost_conf_root / "example.com" / "vhconf.conf").read_text()
    assert "lsphp82" in vhconf
    assert "shop.example.com" in (paths.tela_conf_dir / "zz-listeners.conf").read_text()


def test_force_https_needs_certificate(host):
    create()
    with pytest.raises(ActionError, match="SSL certificate"):
        websites.update_site({"domain": "example.com", "force_https": True})


def test_issue_ssl_and_force_https(host):
    fake, paths = host
    create(aliases=["shop.example.com"])
    fake.dns = {
        "example.com": {"203.0.113.5"},
        "www.example.com": {"203.0.113.5"},
        "shop.example.com": {"198.51.100.1"},
    }
    fake.elsewhere = {"shop.example.com"}  # resolves, but to another server: skipped
    site = websites.issue_ssl({"domain": "example.com"})
    assert site["ssl_names"] == ["example.com", "www.example.com"]
    assert site["skipped_names"] == ["shop.example.com"]
    assert fake.issued[0][2] == paths.sites_root / "example.com" / ".acme"

    vhconf = (paths.vhost_conf_root / "example.com" / "vhconf.conf").read_text()
    assert "vhssl" in vhconf and "fullchain.pem" in vhconf
    assert "RewriteRule" not in vhconf

    websites.update_site({"domain": "example.com", "force_https": True})
    vhconf = (paths.vhost_conf_root / "example.com" / "vhconf.conf").read_text()
    assert "RewriteCond %{HTTPS} !=on" in vhconf
    assert "acme-challenge" in vhconf


def test_issue_ssl_requires_dns(host):
    create()
    with pytest.raises(ActionError, match="does not resolve"):
        websites.issue_ssl({"domain": "example.com"})


def test_issue_ssl_explains_cloudflare(host):
    fake, paths = host
    create("cvgalerij.nl")
    fake.dns = {"cvgalerij.nl": {"2606:4700:3033::6815:116f"}}
    fake.elsewhere = {"cvgalerij.nl"}
    with pytest.raises(ActionError, match="Cloudflare"):
        websites.issue_ssl({"domain": "cvgalerij.nl"})
    assert fake.issued == []  # Let's Encrypt is never asked
    challenge = paths.sites_root / "cvgalerij.nl" / ".acme" / ".well-known" / "acme-challenge"
    assert list(challenge.iterdir()) == []  # test file cleaned up


def test_issue_ssl_wrong_server(host):
    fake, _ = host
    create()
    fake.dns = {"example.com": {"198.51.100.7"}}
    fake.elsewhere = {"example.com"}
    with pytest.raises(ActionError, match="A \\(and AAAA\\) record"):
        websites.issue_ssl({"domain": "example.com"})


def test_issue_ssl_local_web_server_problem(host):
    fake, _ = host
    create()
    fake.dns = {"example.com": {"203.0.113.5"}}
    fake.local_broken = True
    with pytest.raises(ActionError, match="on this machine"):
        websites.issue_ssl({"domain": "example.com"})


def test_delete_site(host):
    fake, paths = host
    create("keep.com")
    create("gone.com")
    websites.delete_site({"domain": "gone.com"})
    assert not (paths.tela_conf_dir / "vhost-gone.com.conf").exists()
    assert not (paths.vhost_conf_root / "gone.com").exists()
    assert not (paths.sites_root / "gone.com").exists()
    assert "gone.com" not in (paths.tela_conf_dir / "zz-listeners.conf").read_text()
    assert "keep.com" in (paths.tela_conf_dir / "zz-listeners.conf").read_text()
    assert "gone" not in fake.users
    assert [s["domain"] for s in websites.list_sites({})] == ["keep.com"]


def test_delete_can_keep_files(host):
    _, paths = host
    create()
    websites.delete_site({"domain": "example.com", "remove_files": False})
    assert (paths.sites_root / "example.com" / "public_html").exists()


def test_strip_blocks_handles_heredoc_braces():
    text = (
        "virtualhost A {\n  rewrite {\n    rules <<<END\nRewriteCond %{HTTPS} on\n}\nEND\n  }\n}\n"
        "listener Keep {\n  address *:1\n}\n"
    )
    result = ols.strip_blocks(text, lambda kind, name: name == "A")
    assert result == "listener Keep {\n  address *:1\n}\n"


def test_htaccess_change_reloads_web_server(host):
    import os

    fake, paths = host
    websites.create_site({"domain": "shop.nl", "php_version": "8.3"})
    watch = websites.HtaccessWatch()
    assert watch.check(paths) is False  # first look only remembers
    before = fake.reloads
    htaccess = websites._load_site(paths, "shop.nl").home / "public_html" / ".htaccess"
    htaccess.write_text("RewriteEngine On\n")
    assert watch.check(paths) is True and fake.reloads == before + 1
    assert watch.check(paths) is False
    os.utime(htaccess, ns=(1, 1))
    assert watch.check(paths) is True
