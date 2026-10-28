from __future__ import annotations

import pytest

from tela_agent import cloudflare, dnsrecords
from tela_agent.registry import ActionError

Z = "example.com"


def v(**record):
    return dnsrecords.validate(record, Z)


@pytest.mark.parametrize(
    "name, expected",
    [("@", "@"), ("", "@"), ("example.com", "@"), ("www", "www"), ("WWW.example.com.", "www"),
     ("*.dev", "*.dev"), ("a.b", "a.b")],
)  # fmt: skip
def test_relative_names(name, expected):
    assert dnsrecords.relative_name(name, Z) == expected


@pytest.mark.parametrize("name", ["a b", "-x", "x..y", "dev.*", "x;y", "a" * 64])
def test_bad_names(name):
    with pytest.raises(ActionError):
        dnsrecords.relative_name(name, Z)


def test_valid_records():
    assert v(type="a", name="www", content=" 203.0.113.5 ")["content"] == "203.0.113.5"
    assert v(type="AAAA", content="2001:DB8::1")["content"] == "2001:db8::1"
    mx = v(type="MX", content="Mail.Example.com.", priority=5)
    assert (mx["content"], mx["priority"]) == ("mail.example.com", 5)
    assert v(type="SRV", name="_sip._tcp", content="5 5060 sip.example.com", priority=10)
    caa = '0 issue "letsencrypt.org"'
    assert v(type="CAA", content=caa)["content"] == caa
    assert v(type="TXT", content="v=spf1 a mx ~all")["ttl"] == 3600


@pytest.mark.parametrize(
    "record",
    [
        {"type": "A", "content": "999.1.1.1"},
        {"type": "AAAA", "content": "1.2.3.4"},
        {"type": "CNAME", "content": "other.net"},  # on the apex
        {"type": "MX", "content": "mail.example.com", "priority": 70000},
        {"type": "SRV", "content": "5 sip.example.com", "priority": 1},
        {"type": "CAA", "content": "0 issue letsencrypt.org"},
        {"type": "TXT", "content": "a\nb"},
        {"type": "PTR", "content": "x"},
        {"type": "A", "content": "1.2.3.4", "ttl": 5},
    ],
)
def test_invalid_records(record):
    with pytest.raises(ActionError):
        dnsrecords.validate(record, Z)


def test_proxied_only_on_cloudflare():
    assert v(type="A", content="1.2.3.4", proxied=True)["proxied"] is False
    record = {"type": "A", "content": "1.2.3.4", "proxied": True}
    r = dnsrecords.validate(record, Z, cloudflare=True)
    assert r["proxied"] is True and r["ttl"] == 1


def test_serial_increases():
    assert dnsrecords.next_serial(0, now=0) == 1970010100
    assert dnsrecords.next_serial(1970010105, now=0) == 1970010106


def test_render_zone():
    records = [
        v(type="A", content="203.0.113.5"),
        v(type="CNAME", name="blog", content="example.net"),
        v(type="MX", content="mail.example.com", priority=10),
        v(type="TXT", name="_dmarc", content='v=DMARC1; p=none; rua="x"'),
        v(type="TXT", name="long", content="k" * 600),
        v(type="SRV", name="_sip._tcp", content="5 5060 sip.example.com", priority=10),
    ]
    text = dnsrecords.render_zone(Z, records, 2026102801, ["ns1.host.nl", "ns2.host.nl"])
    assert "$ORIGIN example.com." in text
    assert "@ IN SOA ns1.host.nl. hostmaster.example.com. ( 2026102801" in text
    assert "@ 3600 IN NS ns2.host.nl." in text
    assert "@ 3600 IN A 203.0.113.5" in text
    assert "blog 3600 IN CNAME example.net." in text
    assert "@ 3600 IN MX 10 mail.example.com." in text
    assert '_dmarc 3600 IN TXT "v=DMARC1; p=none; rua=\\"x\\""' in text
    assert text.count('"k') == 3  # split into 250-character strings
    assert "_sip._tcp 3600 IN SRV 10 5 5060 sip.example.com." in text


def test_cloudflare_conversion_round_trip():
    for record in [
        v(type="SRV", name="_sip._tcp", content="5 5060 sip.example.com", priority=10),
        v(type="CAA", content='0 issue "letsencrypt.org"'),
        v(type="MX", content="mail.example.com", priority=10),
        v(type="A", name="www", content="1.2.3.4"),
    ]:
        body = cloudflare.to_cloudflare(record, Z)
        row = {**body, "id": "f" * 32, "content": body.get("content", "")}
        back = cloudflare.from_cloudflare(row, Z)
        assert (back["name"], back["type"], back["content"], back["priority"]) == (
            record["name"], record["type"], record["content"], record["priority"],
        )  # fmt: skip
