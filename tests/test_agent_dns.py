from __future__ import annotations

import pytest

from tela_agent.actions import dns, websites
from tela_agent.registry import ActionError


@pytest.fixture
def dnshost(host):
    nameservers = ["ns1.host.nl", "ns2.host.nl"]
    dns.update_settings({"public_ipv4": "203.0.113.5", "nameservers": nameservers})
    return host


def test_settings_validation(host):
    with pytest.raises(ActionError):
        dns.update_settings({"public_ipv4": "nope"})
    with pytest.raises(ActionError, match="Invalid API Token"):
        dns.update_settings({"cloudflare_token": "bad-token"})
    result = dns.update_settings({"cloudflare_token": "good-token"})
    assert result["cloudflare_connected"] and result["cloudflare_token_hint"] == "…oken"
    assert "good-token" not in str(dns.status({}))


def test_local_zone_gets_default_records(dnshost):
    fake, _ = dnshost
    dns.create_zone({"zone": "shop.nl", "provider": "local"})
    records = dns.list_records({"zone": "shop.nl"})["records"]
    assert {(r["name"], r["type"], r["content"]) for r in records} == {
        ("@", "A", "203.0.113.5"), ("www", "A", "203.0.113.5"),
    }  # fmt: skip
    text = fake.powerdns.files["shop.nl"]
    assert "@ 3600 IN NS ns1.host.nl." in text
    assert fake.powerdns.reloads[-1] == "shop.nl"


def test_local_zone_needs_nameservers(host):
    with pytest.raises(ActionError, match="nameservers"):
        dns.create_zone({"zone": "shop.nl", "provider": "local"})


def test_local_zone_needs_powerdns(dnshost):
    fake, _ = dnshost
    fake.powerdns.available = False
    with pytest.raises(ActionError, match="--with-dns-server"):
        dns.create_zone({"zone": "shop.nl", "provider": "local"})


def test_record_crud_and_serial(dnshost):
    fake, paths = dnshost
    dns.create_zone({"zone": "shop.nl", "provider": "local", "with_defaults": False})
    import json

    serial1 = json.loads((paths.dns_dir / "zones" / "shop.nl.json").read_text())["serial"]
    mx = dns.create_record({"zone": "shop.nl", "record": {"type": "MX", "content": "mx.shop.nl",
                                                          "priority": 10}})  # fmt: skip
    serial2 = json.loads((paths.dns_dir / "zones" / "shop.nl.json").read_text())["serial"]
    assert serial2 > serial1
    dns.update_record({"zone": "shop.nl", "record": {**mx, "priority": 20}})
    assert "MX 20 mx.shop.nl." in fake.powerdns.files["shop.nl"]
    dns.delete_record({"zone": "shop.nl", "id": mx["id"]})
    assert "MX" not in fake.powerdns.files["shop.nl"]


def test_cname_conflicts(dnshost):
    dns.create_zone({"zone": "shop.nl", "provider": "local"})
    with pytest.raises(ActionError, match="must be alone"):
        dns.create_record({"zone": "shop.nl", "record": {"type": "CNAME", "name": "www",
                                                         "content": "x.net"}})  # fmt: skip
    with pytest.raises(ActionError, match="already exists"):
        dns.create_record({"zone": "shop.nl", "record": {"type": "A", "name": "www",
                                                         "content": "203.0.113.5"}})  # fmt: skip


def test_failed_reload_restores_zone(dnshost):
    fake, _ = dnshost
    dns.create_zone({"zone": "shop.nl", "provider": "local"})
    fake.powerdns.fail_reload = True
    with pytest.raises(ActionError, match="PowerDNS"):
        dns.create_record({"zone": "shop.nl", "record": {"type": "TXT", "content": "hello"}})
    fake.powerdns.fail_reload = False
    types = [r["type"] for r in dns.list_records({"zone": "shop.nl"})["records"]]
    assert "TXT" not in types


def test_cloudflare_zone(dnshost):
    fake, _ = dnshost
    dns.update_settings({"cloudflare_token": "good-token"})
    zones = dns.cloudflare_zones({})
    assert [z["name"] for z in zones] == ["example.com", "cvgalerij.nl"]
    with pytest.raises(ActionError, match="isn't a zone"):
        dns.create_zone({"zone": "other.nl", "provider": "cloudflare", "cf_zone_id": "a" * 32})
    dns.create_zone({"zone": "cvgalerij.nl", "provider": "cloudflare", "cf_zone_id": "b" * 32})
    records = dns.list_records({"zone": "cvgalerij.nl"})["records"]
    assert {(r["name"], r["content"]) for r in records} == {
        ("@", "203.0.113.5"), ("www", "203.0.113.5"),
    }  # fmt: skip
    assert dns.cloudflare_zones({})[1]["linked"] is True
    shop = {"type": "A", "name": "shop", "content": "1.2.3.4", "proxied": True}
    rec = dns.create_record({"zone": "cvgalerij.nl", "record": shop})
    assert rec["proxied"] is True
    dns.delete_record({"zone": "cvgalerij.nl", "id": rec["id"]})
    # Unlinking leaves the zone in Cloudflare.
    dns.delete_zone({"zone": "cvgalerij.nl"})
    assert len(fake.cloudflare.data["b" * 32]) == 2
    with pytest.raises(ActionError, match="no DNS zone"):
        dns.list_records({"zone": "cvgalerij.nl"})


def test_new_website_gets_records(dnshost):
    dns.create_zone({"zone": "shop.nl", "provider": "local", "with_defaults": False})
    site = websites.create_site({"domain": "blog.shop.nl", "php_version": "8.3"})
    assert site["dns"] == "added A blog.shop.nl, A www.blog.shop.nl"
    names = {r["name"] for r in dns.list_records({"zone": "shop.nl"})["records"]}
    assert names == {"blog", "www.blog"}
    again = websites.create_site({"domain": "other.nl", "php_version": "8.3"})
    assert again["dns"] == "no DNS zone for this domain"


def test_disconnect_cloudflare_blocked_while_in_use(dnshost):
    dns.update_settings({"cloudflare_token": "good-token"})
    dns.create_zone({"zone": "cvgalerij.nl", "provider": "cloudflare", "cf_zone_id": "b" * 32})
    with pytest.raises(ActionError, match="remove the Cloudflare zones"):
        dns.update_settings({"cloudflare_token": ""})


def test_nameserver_change_reloads_local_zones(dnshost):
    fake, _ = dnshost
    dns.create_zone({"zone": "shop.nl", "provider": "local"})
    dns.update_settings({"nameservers": ["ns1.other.nl"]})
    assert "@ 3600 IN NS ns1.other.nl." in fake.powerdns.files["shop.nl"]
    assert fake.powerdns.reloads[-1] == "shop.nl"
