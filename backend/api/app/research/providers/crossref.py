"""Crossref REST API research provider for scholarly abstracts.

This adapter retrieves bibliographic metadata and abstracts only. Records
without a substantive abstract are skipped: a paper title alone is not
treated as factual evidence. DOI links are provenance identifiers and are
never fetched by this provider.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import quote, urlencode

from app.research.contracts import EvidenceExcerpt, ResearchDocument
from app.research.normalization import normalize_source_url
from app.research.providers.http import SafeResearchHttpClient
from app.research.safe_http import ResearchFetchError
from app.rights.contracts import (
    ReuseRestriction,
    RightsMetadata,
    RightsScope,
    RightsStatus,
)

_API_ENDPOINT = "https://api.crossref.org/works"
_ALLOWED_HOSTS = frozenset({"api.crossref.org"})
_MAX_RESULTS = 5
_MAX_ABSTRACT_CHARS = 10_000
_MAX_TOPIC_CHARS = 300
_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")


class _AbstractTextParser(HTMLParser):
    """Extract readable text from Crossref's JATS-like abstract markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style"}:
            self._suppressed_depth += 1
        elif tag.casefold() in {"p", "div", "br", "li"} and not self._suppressed_depth:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._suppressed_depth:
            self._suppressed_depth -= 1
        elif tag.casefold() in {"p", "div", "li"} and not self._suppressed_depth:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self.parts.append(data)


def _plain_abstract(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _AbstractTextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception as exc:
        raise ResearchFetchError("Crossref returned an invalid abstract.") from exc
    return " ".join("".join(parser.parts).split())[:_MAX_ABSTRACT_CHARS]


def _published_at(item: dict) -> datetime | None:
    for field in ("published-online", "published-print", "published"):
        date = item.get(field)
        parts = date.get("date-parts") if isinstance(date, dict) else None
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
            continue
        values = parts[0]
        if not values or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in values[:3]
        ):
            continue
        try:
            year = values[0]
            month = values[1] if len(values) > 1 else 1
            day = values[2] if len(values) > 2 else 1
            return datetime(year, month, day, tzinfo=UTC)
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _authors(value: object) -> tuple[str, ...]:
    """Return bounded author display names without inferring copyright ownership."""
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value[:50]:
        if not isinstance(item, dict):
            continue
        explicit_name = item.get("name")
        given = item.get("given")
        family = item.get("family")
        if isinstance(explicit_name, str) and explicit_name.strip():
            name = explicit_name.strip()
        else:
            name = " ".join(
                part.strip()
                for part in (given, family)
                if isinstance(part, str) and part.strip()
            )
        if name and len(name) <= 200 and name not in names:
            names.append(name)
    return tuple(names)


def _decode_response(body: bytes, content_type: str) -> list[dict]:
    if content_type != "application/json":
        raise ResearchFetchError("Crossref returned an unsupported content type.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchFetchError("Crossref returned invalid JSON.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
        raise ResearchFetchError("Crossref returned an invalid response.")
    items = payload["message"].get("items")
    if not isinstance(items, list):
        raise ResearchFetchError("Crossref returned an invalid work list.")
    return [item for item in items if isinstance(item, dict)]


class CrossrefResearchProvider:
    """Retrieve up to five scholarly works with substantive abstracts."""

    def __init__(self, proxy_url: str | None = None) -> None:
        self._proxy_url = proxy_url

    async def research(self, topic: str) -> tuple[ResearchDocument, ...]:
        normalized_topic = topic.strip()
        if not normalized_topic:
            raise ValueError("Research topic must not be empty.")

        query = urlencode({
            "query.bibliographic": normalized_topic[:_MAX_TOPIC_CHARS],
            "rows": str(_MAX_RESULTS),
            "select": (
                "DOI,title,publisher,abstract,published-online,"
                "published-print,published,type,author"
            ),
        })
        url = f"{_API_ENDPOINT}?{query}"
        documents: list[ResearchDocument] = []

        async with asyncio.timeout(45):
            client_options = (
                {"proxy_url": self._proxy_url}
                if self._proxy_url is not None
                else {}
            )
            client = SafeResearchHttpClient(
                allowed_hosts=_ALLOWED_HOSTS,
                **client_options,
            )
            try:
                body, content_type, retrieved_at = await client.get(url)
                items = _decode_response(body, content_type)
                for item in items[:_MAX_RESULTS]:
                    doi = item.get("DOI")
                    titles = item.get("title")
                    publisher = item.get("publisher")
                    if (
                        not isinstance(doi, str)
                        or len(doi) > 255
                        or not _DOI_PATTERN.fullmatch(doi)
                        or not isinstance(titles, list)
                        or not titles
                        or not isinstance(titles[0], str)
                        or not titles[0].strip()
                    ):
                        continue

                    abstract = _plain_abstract(item.get("abstract"))
                    if len(abstract) < 80:
                        # Bibliographic metadata and titles are not evidence.
                        continue

                    title = titles[0].strip()[:500]
                    publisher_name = (
                        publisher.strip()[:250]
                        if isinstance(publisher, str) and publisher.strip()
                        else "Crossref"
                    )
                    authors = _authors(item.get("author"))
                    canonical = normalize_source_url(
                        f"https://doi.org/{quote(doi, safe='/():;._-')}"
                    )
                    rights_metadata = RightsMetadata(
                        status=RightsStatus.REVIEW_REQUIRED,
                        scope=RightsScope.ABSTRACT,
                        creators=authors,
                        attribution_text=(
                            f"{title} — {', '.join(authors)}; DOI {doi}"
                            if authors
                            else f"{title} — DOI {doi}"
                        ),
                        reuse_restrictions=(
                            ReuseRestriction.NO_VERBATIM_REUSE_WITHOUT_LICENSE,
                        ),
                    )
                    documents.append(
                        ResearchDocument(
                            canonical_url=canonical,
                            title=title,
                            publisher=publisher_name,
                            source_type="paper",
                            published_at=_published_at(item),
                            retrieved_at=retrieved_at,
                            content=abstract,
                            evidence=(EvidenceExcerpt(
                                excerpt=abstract,
                                locator=f"Abstract, DOI {doi}",
                            ),),
                            rights_metadata=rights_metadata,
                        )
                    )
            finally:
                await client.close()

        return tuple(documents)
