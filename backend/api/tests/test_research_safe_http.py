import asyncio
import socket

import pytest

from app.research.safe_http import (
    PublicOnlyResolver,
    ResearchFetchError,
    UnsafeResearchTarget,
    redirect_target,
    validate_research_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/resource",
        "http://user:pass@example.com/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "http://192.0.0.9/",
        "http://[64:ff9b::808:808]/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://localhost/",
        "http://service.local/",
        "http://service.internal/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://example.com:8080/",
        "http://example.com:8443/",
        "http://example.com/%0d%0aHost:evil",
    ],
)
def test_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(UnsafeResearchTarget):
        validate_research_url(url)


def test_accepts_and_normalizes_idn_hostname():
    parsed = validate_research_url("https://bücher.example/article#section")
    assert parsed.scheme == "https"
    assert parsed.hostname == "xn--bcher-kva.example"
    assert parsed.fragment == ""


def test_accepts_public_ip_literals_only():
    assert validate_research_url("https://8.8.8.8/").hostname == "8.8.8.8"
    assert validate_research_url("https://[2606:4700:4700::1111]/").hostname == (
        "2606:4700:4700::1111"
    )


def test_rejects_malformed_url_and_port():
    for url in ("https://example.com:bad/", "https://", "https://exa mple.com/"):
        with pytest.raises(UnsafeResearchTarget):
            validate_research_url(url)


def test_redirect_target_is_validated_before_following():
    assert redirect_target(
        "https://example.com/path",
        "/next",
        0,
    ) == "https://example.com/next"

    with pytest.raises(ResearchFetchError):
        redirect_target("https://example.com/", "http://127.0.0.1/", 0)

    with pytest.raises(ResearchFetchError, match="redirect limit"):
        redirect_target("https://example.com/", "/next", 3)


def test_resolver_rejects_mixed_public_and_private_dns_answers(monkeypatch):
    async def fake_getaddrinfo(self, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.8", 443)),
        ]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UnsafeResearchTarget):
        asyncio.run(PublicOnlyResolver().resolve("mixed.example", 443))


def test_resolver_returns_only_the_validated_addresses(monkeypatch):
    async def fake_getaddrinfo(self, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443)),
        ]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)

    result = asyncio.run(PublicOnlyResolver().resolve("public.example", 443))
    assert {item["host"] for item in result} == {"8.8.8.8", "1.1.1.1"}
    assert all(item["flags"] == socket.AI_NUMERICHOST for item in result)


def test_resolver_rejects_metadata_ip_literal():
    with pytest.raises(UnsafeResearchTarget):
        asyncio.run(PublicOnlyResolver().resolve("169.254.169.254", 80))
