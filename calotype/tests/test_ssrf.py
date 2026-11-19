"""Unit tests for the address checker. No network: DNS goes through a fake resolver."""

from __future__ import annotations

import asyncio
import ipaddress

import pytest

from calotype.ssrf import AddressPolicy, BlockedTarget, is_blocked_ip, parse_ip_literal, parse_target_url

from .conftest import fake_resolver

BLOCKED_IPS = [
    "0.0.0.0",
    "0.1.2.3",
    "10.0.0.1",
    "10.255.255.255",
    "100.64.0.1",
    "100.100.100.200",  # Alibaba Cloud metadata
    "127.0.0.1",
    "127.255.255.254",
    "169.254.169.254",  # AWS/GCP/Azure metadata
    "169.254.0.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.0.0.1",
    "192.0.2.10",
    "192.88.99.1",
    "192.168.1.1",
    "198.18.0.1",
    "198.51.100.7",
    "203.0.113.9",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "::127.0.0.1",
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b:1::1",
    "100::1",
    "2001::1",  # Teredo
    "2001:db8::1",
    "2002:7f00:1::1",  # 6to4 of 127.0.0.1
    "fc00::1",
    "fd00::1",
    "fd00:ec2::254",  # AWS IPv6 metadata
    "fe80::1",
    "fe80::1%1",
    "fec0::1",
    "ff02::1",
]

ALLOWED_IPS = [
    "1.1.1.1",
    "8.8.8.8",
    "93.184.215.14",
    "172.15.255.255",  # just below 172.16/12
    "172.32.0.1",  # just above
    "100.63.255.255",  # just below CGNAT
    "100.128.0.1",  # just above CGNAT
    "192.169.0.1",
    "2606:4700:4700::1111",
    "2a00:1450:4001:80b::200e",
    "64:ff9b::808:808",  # NAT64 of 8.8.8.8
]


@pytest.mark.parametrize("ip", BLOCKED_IPS)
def test_blocked_ips(ip):
    assert is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ALLOWED_IPS)
def test_allowed_ips(ip):
    assert is_blocked_ip(ip) is False


@pytest.mark.parametrize(
    "literal,expected",
    [
        ("127.0.0.1", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),  # decimal
        ("0x7f000001", "127.0.0.1"),  # hex
        ("0x7f.1", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),  # octal
        ("127.1", "127.0.0.1"),
        ("[::1]", "::1"),
        ("[::ffff:7f00:1]", "::ffff:127.0.0.1"),
        ("169.254.169.254.", "169.254.169.254"),
    ],
)
def test_ip_literal_forms(literal, expected):
    assert str(parse_ip_literal(literal)) == expected


@pytest.mark.parametrize("name", ["example.com", "cafe.de", "1e100.net", "123.com", "dead.beef"])
def test_names_are_not_literals(name):
    assert parse_ip_literal(name) is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<h1>x</h1>",
        "chrome://settings",
        "chrome-extension://abc/x.html",
        "javascript:alert(1)",
        "about:blank",
        "view-source:http://example.com",
        "ftp://example.com/",
        "gopher://example.com/",
        "ws://example.com/",
        "blob:http://example.com/uuid",
        "filesystem:http://example.com/temporary/x",
        "//example.com/",
        "example.com",
        "",
        "http://",
        "http://localhost/",
        "http://LOCALHOST./",
        "http://foo.localhost/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata/",
        "http://printer.local/",
        "http://intranet/",
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd00:ec2::254]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.0.1/",
        "http://0.0.0.0/",
        "http://user:pass@example.com/",
        "http://example.com@127.0.0.1/",
        "http://example.com:22/",
        "http://example.com:25/",
        "http://example.com:0/",
        "http://example.com:99999/",
        "http://exa mple.com/",
        "http://example.com\\@127.0.0.1/",
        "http://example.com/" + "a" * 2100,
    ],
)
def test_parse_target_url_refuses(url):
    with pytest.raises(BlockedTarget):
        parse_target_url(url)


@pytest.mark.parametrize(
    "url,host,port",
    [
        ("https://example.com/", "example.com", 443),
        ("http://example.com/path?q=1", "example.com", 80),
        ("HTTPS://EXAMPLE.COM:8443/x", "example.com", 8443),
        ("http://93.184.215.14/", "93.184.215.14", 80),
        ("https://[2606:4700:4700::1111]/", "2606:4700:4700::1111", 443),
        ("https://bücher.example/", "xn--bcher-kva.example", 443),
    ],
)
def test_parse_target_url_accepts(url, host, port):
    t = parse_target_url(url)
    assert (t.host, t.port) == (host, port)
    assert "?" not in t.display and "/" not in t.display.split("://", 1)[1]


def _check(url, policy=None):
    policy = policy or AddressPolicy(resolver=fake_resolver)
    return asyncio.run(policy.check_url(url))


@pytest.mark.parametrize(
    "url",
    [
        "http://rebind.example/",  # a public-looking name that resolves to loopback
        "http://mixed.example/",  # one public and one private record
        "http://metadata.example/",
        "http://v6local.example/",
        "http://mapped.example/",
        "http://nxdomain.example/",
    ],
)
def test_policy_refuses_after_resolution(url):
    with pytest.raises(BlockedTarget):
        _check(url)


def test_policy_accepts_public_name():
    assert _check("https://public.example/").host == "public.example"


def test_policy_exception_is_exact():
    policy = AddressPolicy(resolver=fake_resolver, exceptions=frozenset({("127.0.0.1", 5555)}))
    assert _check("http://127.0.0.1:5555/", policy).port == 5555
    with pytest.raises(BlockedTarget):
        _check("http://127.0.0.1:5556/", policy)
    with pytest.raises(BlockedTarget):
        _check("http://localhost:5555/", policy)


def test_policy_dns_timeout():
    async def slow(host, port):
        await asyncio.sleep(5)
        return [ipaddress.ip_address("1.1.1.1")]

    with pytest.raises(BlockedTarget):
        _check("http://slow.example/", AddressPolicy(resolver=slow, resolve_timeout_s=0.05))
