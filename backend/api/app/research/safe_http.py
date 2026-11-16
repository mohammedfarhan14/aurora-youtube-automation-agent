"""SSRF-resistant asynchronous HTTP retrieval primitives.

The resolver validates every DNS answer and returns only those exact IP
addresses to aiohttp's connector. aiohttp connects to the returned address
while retaining the URL hostname for Host and TLS SNI/certificate checks.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import SplitResult, urljoin, urlsplit

from aiohttp.abc import AbstractResolver

_MAX_REDIRECTS = 3
_ALLOWED_PORTS = {80, 443}
_AMBIGUOUS_NUMERIC_HOST = re.compile(r"^[0-9.]+$")
_BLOCKED_SPECIAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "192.0.0.0/24",
        "192.88.99.0/24",
        "2001::/23",
        "2001:db8::/32",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
    )
)
_BLOCKED_METADATA = {
    ipaddress.ip_address("168.63.129.16"),
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}


class UnsafeResearchTarget(ValueError):
    """A URL or resolved destination violates outbound-fetch policy."""


class ResearchFetchError(RuntimeError):
    """A bounded, sanitized retrieval failure."""


def _validate_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError as exc:
        raise UnsafeResearchTarget("Research destination is not a valid IP address.") from exc

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise UnsafeResearchTarget("IPv4-mapped IPv6 destinations are not permitted.")

    if (
        address in _BLOCKED_METADATA
        or any(
            address.version == network.version and address in network
            for network in _BLOCKED_SPECIAL_NETWORKS
        )
        or address.is_reserved
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or not address.is_global
    ):
        raise UnsafeResearchTarget("Research destination is not globally routable.")

    return address


def validate_research_url(value: str) -> SplitResult:
    """Validate and normalize an HTTP(S) URL before any network operation."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise UnsafeResearchTarget("Research URL is invalid.")

    has_control = any(ord(char) < 0x20 or char.isspace() for char in value)
    has_encoded_control = re.search(r"%(?:0d|0a|00)", value, re.IGNORECASE)
    if has_control or has_encoded_control:
        raise UnsafeResearchTarget("Research URL contains invalid whitespace.")

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeResearchTarget("Research URL is malformed.") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise UnsafeResearchTarget("Only HTTP and HTTPS research URLs are supported.")

    if parsed.username is not None or parsed.password is not None:
        raise UnsafeResearchTarget("Research URLs must not contain credentials.")

    if parsed.fragment:
        parsed = parsed._replace(fragment="")

    if parsed.scheme.lower() == "https" and port not in (None, 443):
        raise UnsafeResearchTarget("Only the standard HTTPS port is supported.")
    if parsed.scheme.lower() == "http" and port not in (None, 80):
        raise UnsafeResearchTarget("Only the standard HTTP port is supported.")
    if port is not None and port not in _ALLOWED_PORTS:
        raise UnsafeResearchTarget("Research URL port is not supported.")

    if "%" in hostname:
        raise UnsafeResearchTarget("Scoped or escaped hostnames are not supported.")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if _AMBIGUOUS_NUMERIC_HOST.fullmatch(hostname):
            raise UnsafeResearchTarget("Ambiguous numeric host literals are not supported.")
        try:
            ascii_host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UnsafeResearchTarget("Research hostname is invalid.") from exc
        if (
            not ascii_host
            or len(ascii_host) > 253
            or any(not label or len(label) > 63 for label in ascii_host.split("."))
            or ascii_host == "localhost"
            or ascii_host.endswith((".localhost", ".local", ".internal", ".test"))
        ):
            raise UnsafeResearchTarget("Local or malformed research hostnames are not permitted.")
        netloc_host = ascii_host
    else:
        _validate_ip(str(address))
        netloc_host = f"[{address.compressed}]" if address.version == 6 else str(address)

    if parsed.port is not None:
        netloc = f"{netloc_host}:{parsed.port}"
    else:
        netloc = netloc_host

    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=netloc,
        path=parsed.path or "/",
    )


class PublicOnlyResolver(AbstractResolver):
    """Resolve once per new connection and reject the entire answer set if unsafe."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_UNSPEC,
    ) -> list[dict[str, object]]:
        if not host or "%" in host:
            raise OSError("Research hostname is invalid.")

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            if _AMBIGUOUS_NUMERIC_HOST.fullmatch(host):
                raise OSError("Ambiguous numeric host is not permitted.")
        else:
            _validate_ip(str(literal))
            return [{
                "hostname": host,
                "host": str(literal),
                "port": port,
                "family": socket.AF_INET6 if literal.version == 6 else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }]

        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host,
                port,
                family=family,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError:
            raise

        addresses: dict[tuple[int, str], dict[str, object]] = {}
        for record_family, socktype, proto, _canonname, sockaddr in records:
            address = _validate_ip(sockaddr[0])
            key = (record_family, address.compressed)
            addresses[key] = {
                "hostname": host,
                "host": address.compressed,
                "port": port,
                "family": record_family,
                "proto": proto,
                "flags": socket.AI_NUMERICHOST,
            }

        if not addresses:
            raise OSError("Research hostname did not resolve to a usable address.")

        return list(addresses.values())

    async def close(self) -> None:
        return None


def validate_research_proxy_url(value: str | None) -> str | None:
    """Accept only the internal, domain-allowlisted research egress proxy."""
    if value is None or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Research egress proxy URL is invalid.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "research-egress-proxy"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port != 3128
    ):
        raise ValueError("Research must use the approved internal egress proxy.")
    return value.strip().rstrip("/")


def redirect_target(current_url: str, location: str, redirects_followed: int) -> str:
    """Resolve a redirect and validate its target before the next connection."""
    if redirects_followed >= _MAX_REDIRECTS:
        raise ResearchFetchError("Research source exceeded the redirect limit.")
    if not location or len(location) > 4096:
        raise ResearchFetchError("Research source returned an invalid redirect.")
    try:
        target = urljoin(current_url, location)
        validate_research_url(target)
    except (ValueError, UnsafeResearchTarget) as exc:
        raise ResearchFetchError("Research source returned an unsafe redirect.") from exc
    return target
