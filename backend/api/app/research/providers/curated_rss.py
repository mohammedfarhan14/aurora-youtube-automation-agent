"""Operator-curated RSS/Atom trend discovery.

Feed URLs and their exact HTTPS hosts are supplied by the operator. Feed entries
are discovery leads only; this adapter never fetches article links or treats
feed text as factual evidence.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.research.providers.http import SafeResearchHttpClient
from app.research.safe_http import ResearchFetchError, validate_research_url
from app.research.trend_ranking import TrendObservation

_XML_CONTENT_TYPES = frozenset({
    "application/atom+xml",
    "application/rss+xml",
    "application/xml",
    "text/xml",
})
_MAX_FEEDS = 10
_MAX_ENTRIES_PER_FEED = 30
_MAX_TITLE_LENGTH = 500
_MAX_PUBLISHER_LENGTH = 200
_MAX_XML_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class CuratedFeed:
    """An explicitly configured feed and its operator-supplied publisher label."""

    url: str
    publisher: str


class CuratedRssTrendProvider:
    """Fetch bounded RSS/Atom snapshots from explicitly allowlisted feed hosts."""

    def __init__(self, feeds: Iterable[CuratedFeed]) -> None:
        self._feeds = tuple(feeds)
        if not 1 <= len(self._feeds) <= _MAX_FEEDS:
            raise ValueError(f"Configure between 1 and {_MAX_FEEDS} curated feeds.")

        hosts: set[str] = set()
        seen_urls: set[str] = set()
        for feed in self._feeds:
            if not isinstance(feed, CuratedFeed):
                raise TypeError("Curated feeds must use the CuratedFeed contract.")
            if not isinstance(feed.publisher, str) or not feed.publisher.strip():
                raise ValueError("Curated feed publisher must not be empty.")
            if len(feed.publisher) > _MAX_PUBLISHER_LENGTH:
                raise ValueError("Curated feed publisher is too long.")
            parsed = validate_research_url(feed.url)
            if parsed.scheme != "https":
                raise ValueError("Curated feeds must use HTTPS.")
            canonical_url = parsed.geturl()
            if canonical_url in seen_urls:
                raise ValueError("Curated feed URLs must be unique.")
            seen_urls.add(canonical_url)
            hosts.add(parsed.hostname or "")
        self._allowed_hosts = frozenset(hosts)

    async def discover(self) -> tuple[TrendObservation, ...]:
        observations: list[TrendObservation] = []
        async with asyncio.timeout(60):
            client = SafeResearchHttpClient(
                allowed_hosts=self._allowed_hosts,
                allowed_content_types=_XML_CONTENT_TYPES,
            )
            try:
                for feed in self._feeds:
                    body, content_type, retrieved_at = await client.get(feed.url)
                    if len(body) > _MAX_XML_BYTES:
                        raise ResearchFetchError("Curated feed exceeded the XML size limit.")
                    entries = _parse_feed(body, content_type)
                    for entry in entries[:_MAX_ENTRIES_PER_FEED]:
                        title = entry["title"].strip()
                        if not title or len(title) > _MAX_TITLE_LENGTH:
                            continue
                        observed_at = entry["published_at"]
                        if not isinstance(observed_at, datetime):
                            # Undated feed items cannot be assigned a trustworthy
                            # trend age; do not make them appear freshly published.
                            continue
                        observations.append(
                            TrendObservation(
                                provider="curated_rss",
                                topic=title,
                                observed_at=observed_at,
                                retrieved_at=retrieved_at,
                                metric_name="feed_entry",
                                metric_value=1.0,
                                metric_unit="entry",
                                language=None,
                                source_url=feed.url,
                                publisher=feed.publisher.strip(),
                            )
                        )
            finally:
                await client.close()
        return tuple(observations)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            if name == "link":
                href = child.attrib.get("href")
                if href:
                    return href.strip()
            if child.text:
                return child.text.strip()
    return None


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _parse_feed(body: bytes, content_type: str) -> list[dict[str, object]]:
    if content_type not in _XML_CONTENT_TYPES:
        raise ResearchFetchError("Curated feed returned an unsupported content type.")
    if len(body) > _MAX_XML_BYTES:
        raise ResearchFetchError("Curated feed exceeded the XML size limit.")
    try:
        # Accept UTF-8 only so declaration checks cannot be bypassed with a
        # UTF-16/UTF-32 encoded DTD or entity declaration.
        xml_text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResearchFetchError("Curated feed XML must be UTF-8 encoded.") from exc
    upper_text = xml_text.upper()
    if "<!DOCTYPE" in upper_text or "<!ENTITY" in upper_text:
        raise ResearchFetchError("Curated feed contains prohibited XML declarations.")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ResearchFetchError("Curated feed returned invalid XML.") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        entries = [
            child for channel in root
            if _local_name(channel.tag) == "channel"
            for child in channel
            if _local_name(child.tag) == "item"
        ]
    elif root_name == "feed":
        entries = [child for child in root if _local_name(child.tag) == "entry"]
    else:
        raise ResearchFetchError("Curated feed is neither RSS nor Atom.")

    parsed_entries: list[dict[str, object]] = []
    for entry in entries[:_MAX_ENTRIES_PER_FEED]:
        title = _child_text(entry, "title")
        if not title:
            continue
        published = (
            _child_text(entry, "pubdate")
            or _child_text(entry, "published")
            or _child_text(entry, "updated")
        )
        parsed_entries.append({
            "title": title,
            "published_at": _parse_published(published),
        })
    return parsed_entries
