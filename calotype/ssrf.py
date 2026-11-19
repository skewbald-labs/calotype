"""Decide whether a URL or a network address may be fetched.

Three layers use this module:

1. The API checks the requested URL before rendering (scheme, host, port, and
   every address the host resolves to).
2. The page's request router (Playwright) aborts any request with a
   disallowed scheme or a host that is plainly disallowed.
3. The egress proxy (``calotype.egress``) is the only path from Chromium to
   the network. It resolves each host itself, refuses disallowed addresses,
   and connects to the exact address it checked, so redirects, sub-requests,
   WebSockets and DNS rebinding all meet the same rule.

The rule is an allow-list of what is *public*: an address is refused unless it
is a globally routable unicast address outside every special-purpose range.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

ALLOWED_SCHEMES = frozenset({"http", "https"})
# Ports a target may use. Below 1024 only the web ports are allowed, so the
# service cannot be pointed at mail, SSH, databases on their default ports, etc.
ALLOWED_LOW_PORTS = frozenset({80, 443})
MAX_URL_LENGTH = 2048

_V4_BLOCKED = [
    "0.0.0.0/8",  # "this" network
    "10.0.0.0/8",  # private
    "100.64.0.0/10",  # carrier-grade NAT (also Alibaba metadata 100.100.100.200)
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link-local, cloud metadata 169.254.169.254
    "172.16.0.0/12",  # private
    "192.0.0.0/24",  # IETF protocol assignments
    "192.0.2.0/24",  # TEST-NET-1
    "192.31.196.0/24",  # AS112
    "192.52.193.0/24",  # AMT
    "192.88.99.0/24",  # 6to4 relay anycast
    "192.168.0.0/16",  # private
    "192.175.48.0/24",  # AS112
    "198.18.0.0/15",  # benchmarking
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",  # TEST-NET-3
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved, includes 255.255.255.255
]
_V6_BLOCKED = [
    "::/128",  # unspecified
    "::1/128",  # loopback
    "::/96",  # IPv4-compatible (deprecated)
    "::ffff:0:0/96",  # IPv4-mapped (checked separately too)
    "::ffff:0:0:0/96",  # IPv4-translated
    "64:ff9b:1::/48",  # local-use NAT64
    "100::/64",  # discard
    "2001::/23",  # IETF protocol assignments, includes Teredo 2001::/32
    "2001:db8::/32",  # documentation
    "2002::/16",  # 6to4, embeds an IPv4 address
    "3fff::/20",  # documentation
    "5f00::/16",  # SRv6 SIDs
    "fc00::/7",  # unique local, includes fd00::/8 and AWS metadata fd00:ec2::254
    "fe80::/10",  # link-local
    "fec0::/10",  # site-local (deprecated)
    "ff00::/8",  # multicast
]
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(n) for n in _V4_BLOCKED + _V6_BLOCKED
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

# Names refused before any lookup. Resolution would usually catch these, but
# a container's resolver may map them to private addresses we never see.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
BLOCKED_HOST_SUFFIXES = (".localhost", ".internal", ".local", ".localdomain", ".home.arpa", ".lan", ".intranet", ".corp")


class BlockedTarget(Exception):
    """The URL or address may not be fetched. ``reason`` is safe to show users."""

    def __init__(self, reason: str, code: str = "blocked_target"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def is_blocked_ip(ip: IPAddress | str) -> bool:
    """True if the address is anything other than public unicast."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return True
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return is_blocked_ip(mapped)
        if ip in _NAT64:
            return is_blocked_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    for net in BLOCKED_NETWORKS:
        if ip.version == net.version and ip in net:
            return True
    # Belt and braces: anything Python does not consider global is refused too.
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    return False


def parse_ip_literal(host: str) -> IPAddress | None:
    """Parse a host as an IP address, including the legacy IPv4 forms browsers accept.

    ``2130706433``, ``0x7f.1`` and ``0177.0.0.1`` all mean 127.0.0.1 to
    Chromium's URL parser, so they must mean the same here.
    """
    h = host.strip().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        return ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        pass
    if h and all(c in "0123456789abcdefxABCDEFX." for c in h) and any(c.isdigit() for c in h):
        # Only forms whose last label is numeric are IPv4 to a browser; "abc.de" is a name.
        last = h.split(".")[-1].lower()
        if last.isdigit() or last.startswith("0x"):
            try:
                return ipaddress.IPv4Address(socket.inet_aton(h))
            except (OSError, ValueError):
                raise BlockedTarget("The host is not a valid address.", "invalid_url")
    return None


def normalize_host(host: str) -> str:
    h = host.strip().lower().rstrip(".")
    if not h:
        raise BlockedTarget("The URL has no host.", "invalid_url")
    if h.startswith("[") and h.endswith("]"):
        return h
    try:
        # IDNA so lookalike Unicode names map to the name DNS will see.
        h = h.encode("idna").decode("ascii")
    except UnicodeError:
        raise BlockedTarget("The host name is not valid.", "invalid_url")
    return h


def is_blocked_hostname(host: str) -> bool:
    h = host.lower().rstrip(".")
    if h in BLOCKED_HOSTNAMES:
        return True
    return any(h.endswith(s) or h == s.lstrip(".") for s in BLOCKED_HOST_SUFFIXES)


def check_port(port: int) -> None:
    if not (1 <= port <= 65535):
        raise BlockedTarget("The port is not valid.", "invalid_url")
    if port < 1024 and port not in ALLOWED_LOW_PORTS:
        raise BlockedTarget(f"Port {port} is not allowed. Use 80, 443 or a port from 1024 up.")


@dataclass(frozen=True)
class ParsedTarget:
    scheme: str
    host: str  # normalized, IPv6 without brackets
    port: int
    ip_literal: IPAddress | None

    @property
    def display(self) -> str:
        """Scheme and host only. Safe to log: no path, query or credentials."""
        return f"{self.scheme}://{self.host}"


def parse_target_url(url: str) -> ParsedTarget:
    """Check the parts of a URL that need no network. Raises BlockedTarget."""
    if not isinstance(url, str) or not url.strip():
        raise BlockedTarget("A url is required.", "invalid_url")
    if len(url) > MAX_URL_LENGTH:
        raise BlockedTarget(f"The url is longer than {MAX_URL_LENGTH} characters.", "invalid_url")
    if any(ord(c) < 0x21 or c == "\\" for c in url.strip()):
        raise BlockedTarget("The url contains whitespace, control characters or backslashes.", "invalid_url")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise BlockedTarget("The url could not be parsed.", "invalid_url")
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedTarget("Only http and https URLs can be captured.", "invalid_scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BlockedTarget("URLs with credentials (user:pass@host) are not supported.", "invalid_url")
    if not parts.hostname:
        raise BlockedTarget("The URL has no host.", "invalid_url")
    host = normalize_host(parts.hostname)
    if port is None:
        port = 443 if scheme == "https" else 80
    check_port(port)
    literal = parse_ip_literal(host)
    if literal is not None:
        host = str(literal)
        if is_blocked_ip(literal):
            raise BlockedTarget("The address is private, local or reserved, so it cannot be captured.")
    elif is_blocked_hostname(host):
        raise BlockedTarget("That host name points at a private network and cannot be captured.")
    elif "." not in host:
        raise BlockedTarget("The host must be a fully qualified public name.")
    return ParsedTarget(scheme=scheme, host=host, port=port, ip_literal=literal)


Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]


async def system_resolve(host: str, port: int) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[IPAddress] = []
    for family, _t, _p, _c, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6):
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
            if ip not in out:
                out.append(ip)
    return out


@dataclass
class AddressPolicy:
    """The single rule every layer applies.

    ``exceptions`` lets tests reach a server they run on loopback. It holds
    exact (ip, port) pairs and can only be set in code, never from the
    environment, so a deployment cannot switch protection off by config.
    """

    resolver: Resolver = system_resolve
    exceptions: frozenset[tuple[str, int]] = field(default_factory=frozenset)
    resolve_timeout_s: float = 5.0

    def ip_allowed(self, ip: IPAddress, port: int) -> bool:
        if (str(ip), port) in self.exceptions:
            return True
        return not is_blocked_ip(ip)

    async def resolve_checked(self, host: str, port: int) -> list[IPAddress]:
        """Resolve ``host`` and return its addresses, or raise if any is disallowed.

        Refusing when *any* record is private stops a name that mixes public
        and private records from slipping through on a retry.
        """
        check_port_with_exceptions(port, host, self.exceptions)
        literal = parse_ip_literal(host)
        if literal is not None:
            ips = [literal]
        else:
            if is_blocked_hostname(host):
                raise BlockedTarget("That host name points at a private network and cannot be captured.")
            try:
                ips = await asyncio.wait_for(self.resolver(host, port), self.resolve_timeout_s)
            except asyncio.TimeoutError:
                raise BlockedTarget("The host name did not resolve in time.", "dns_error")
            except (OSError, UnicodeError):
                raise BlockedTarget("The host name could not be resolved.", "dns_error")
        if not ips:
            raise BlockedTarget("The host name could not be resolved.", "dns_error")
        for ip in ips:
            if not self.ip_allowed(ip, port):
                raise BlockedTarget("The address is private, local or reserved, so it cannot be captured.")
        return ips

    async def check_url(self, url: str) -> ParsedTarget:
        try:
            target = parse_target_url(url)
        except BlockedTarget as e:
            # A loopback test server is reachable only through an exact exception.
            if not self.exceptions:
                raise
            target = _parse_for_exception(url, self.exceptions, e)
        await self.resolve_checked(target.host, target.port)
        return target


def check_port_with_exceptions(port: int, host: str, exceptions: Iterable[tuple[str, int]]) -> None:
    if any(p == port and h == host for h, p in exceptions):
        return
    check_port(port)


def _parse_for_exception(url: str, exceptions: frozenset[tuple[str, int]], original: BlockedTarget) -> ParsedTarget:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    if scheme in ALLOWED_SCHEMES and (host, port) in exceptions:
        return ParsedTarget(scheme, host, port, parse_ip_literal(host))
    raise original
