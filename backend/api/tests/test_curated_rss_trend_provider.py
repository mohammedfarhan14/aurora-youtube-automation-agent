import asyncio
from datetime import UTC, datetime

import pytest

from app.research.providers import curated_rss


class FakeClient:
    def __init__(self, *, allowed_hosts, allowed_content_types):
        self.allowed_hosts = allowed_hosts
        self.allowed_content_types = allowed_content_types
        self.closed = False
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        body = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item><title>New open source database released</title>
            <pubDate>Thu, 12 Nov 2026 10:00:00 GMT</pubDate></item>
          <item><title>Research breakthrough announced</title></item>
        </channel></rss>"""
        return body, "application/rss+xml", datetime(2026, 11, 12, 11, tzinfo=UTC)

    async def close(self):
        self.closed = True


def test_curated_rss_provider_maps_feed_entries_without_fetching_article_urls(monkeypatch):
    clients = []

    def client_factory(*, allowed_hosts, allowed_content_types):
        client = FakeClient(
            allowed_hosts=allowed_hosts,
            allowed_content_types=allowed_content_types,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(curated_rss, "SafeResearchHttpClient", client_factory)
    provider = curated_rss.CuratedRssTrendProvider((
        curated_rss.CuratedFeed(
            url="https://feeds.example.org/technology.xml",
            publisher="Example Technology Desk",
        ),
    ))
    result = asyncio.run(provider.discover())

    assert len(result) == 1
    assert result[0].provider == "curated_rss"
    assert result[0].topic == "New open source database released"
    assert result[0].observed_at == datetime(2026, 11, 12, 10, tzinfo=UTC)
    assert result[0].source_url == "https://feeds.example.org/technology.xml"
    assert clients[0].allowed_hosts == frozenset({"feeds.example.org"})
    assert clients[0].allowed_content_types == curated_rss._XML_CONTENT_TYPES
    assert clients[0].calls == ["https://feeds.example.org/technology.xml"]
    assert clients[0].closed is True


def test_curated_rss_provider_rejects_http_and_duplicate_feed_urls():
    with pytest.raises(ValueError, match="HTTPS"):
        curated_rss.CuratedRssTrendProvider((
            curated_rss.CuratedFeed("http://feeds.example.org/rss", "Example"),
        ))
    feed = curated_rss.CuratedFeed("https://feeds.example.org/rss", "Example")
    with pytest.raises(ValueError, match="unique"):
        curated_rss.CuratedRssTrendProvider((feed, feed))


def test_curated_rss_provider_requires_bounded_feed_configuration():
    with pytest.raises(ValueError, match="between 1 and 10"):
        curated_rss.CuratedRssTrendProvider(())
    feeds = tuple(
        curated_rss.CuratedFeed(f"https://feed{i}.example.org/rss", f"Publisher {i}")
        for i in range(11)
    )
    with pytest.raises(ValueError, match="between 1 and 10"):
        curated_rss.CuratedRssTrendProvider(feeds)


def test_curated_rss_parser_supports_atom_and_rejects_unsafe_xml():
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>New model release</title>
        <published>2026-11-12T10:30:00Z</published></entry>
    </feed>"""
    entries = curated_rss._parse_feed(atom, "application/atom+xml")
    assert entries[0]["title"] == "New model release"
    assert entries[0]["published_at"] == datetime(2026, 11, 12, 10, 30, tzinfo=UTC)

    with pytest.raises(curated_rss.ResearchFetchError, match="XML declarations"):
        curated_rss._parse_feed(
            b"<!DOCTYPE rss [<!ENTITY x 'boom'>]><rss/>",
            "application/rss+xml",
        )
    utf16_doctype = "<!DOCTYPE rss [<!ENTITY x 'boom'>]><rss/>".encode("utf-16")
    with pytest.raises(curated_rss.ResearchFetchError, match="UTF-8"):
        curated_rss._parse_feed(utf16_doctype, "application/rss+xml")
    with pytest.raises(curated_rss.ResearchFetchError, match="invalid XML"):
        curated_rss._parse_feed(b"<rss>", "application/rss+xml")
