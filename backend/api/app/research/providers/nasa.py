"""Official NASA web search provider for factual source retrieval.

This adapter retrieves NASA-authored article text as research evidence only.
NASA pages may contain third-party material, so source rights remain
review-required and must never be inferred as cleared by the provider.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

from app.research.contracts import EvidenceExcerpt, ResearchDocument
from app.research.providers.http import SafeResearchHttpClient
from app.research.safe_http import ResearchFetchError
from app.rights.contracts import ReuseRestriction, RightsMetadata, RightsScope, RightsStatus

_SEARCH_ENDPOINT = "https://www.nasa.gov/wp-json/wp/v2/search"
_ALLOWED_HOSTS = frozenset({"www.nasa.gov"})
_MAX_RESULTS = 5
_MAX_CONTENT_CHARS = 20_000
_MAX_EVIDENCE_CHARS = 1_600
_MIN_EVIDENCE_CHARS = 100
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "does", "for", "how", "in", "is", "it", "of",
    "on", "the", "to", "what", "with",
})


class _HtmlText(HTMLParser):
    """Extract readable text while ignoring script/style and navigation labels."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._suppressed += 1
        elif tag.casefold() in {"p", "div", "li", "br", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self._suppressed:
            self._suppressed -= 1
        elif tag.casefold() in {"p", "div", "li", "h1", "h2", "h3"} and not self._suppressed:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)


def _decode_json(body: bytes, content_type: str) -> object:
    if content_type != "application/json":
        raise ResearchFetchError("NASA returned an unsupported content type.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchFetchError("NASA returned invalid JSON.") from exc


def _text_blocks(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    parser = _HtmlText()
    try:
        parser.feed(value)
        parser.close()
    except Exception as exc:
        raise ResearchFetchError("NASA returned malformed article content.") from exc
    return [
        " ".join(block.split())[:_MAX_CONTENT_CHARS]
        for block in "".join(parser.parts).splitlines()
        if block.strip()
    ]


def _plain_text(value: object) -> str:
    return " ".join(_text_blocks(value))[:_MAX_CONTENT_CHARS]


def _relevant_evidence(rendered_content: object, topic: str) -> str:
    blocks = _text_blocks(rendered_content)
    sentences = [
        sentence.strip()
        for block in blocks
        for sentence in re.split(r"(?<=[.!?])\s+", block)
        if sentence.strip()
    ]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        words = sentence.split()
        while len(" ".join(words)) > _MAX_EVIDENCE_CHARS:
            part = " ".join(words[:180])
            chunks.append(part[:_MAX_EVIDENCE_CHARS])
            words = words[180:]
        normalized = " ".join(words)
        if current and len(current) + len(normalized) + 1 > _MAX_EVIDENCE_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current} {normalized}".strip()
    if current:
        chunks.append(current)
    if not chunks:
        return ""

    topic_terms = {
        term.casefold()
        for term in re.findall(r"[\w]+", topic)
        if len(term) > 2 and term.casefold() not in _STOPWORDS
    }
    if not topic_terms:
        return chunks[0][:_MAX_EVIDENCE_CHARS]
    best = max(
        enumerate(chunks),
        key=lambda pair: (
            len(topic_terms & {t.casefold() for t in re.findall(r"[\w]+", pair[1])}),
            -pair[0],
        ),
    )[1]
    return best[:_MAX_EVIDENCE_CHARS]


def _safe_nasa_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.geturl()


class NasaResearchProvider:
    """Search and fetch a bounded set of NASA.gov source articles."""

    def __init__(self, proxy_url: str | None = None) -> None:
        self._proxy_url = proxy_url

    async def research(self, topic: str) -> tuple[ResearchDocument, ...]:
        normalized_topic = topic.strip()
        if not normalized_topic:
            raise ValueError("Research topic must not be empty.")

        async with asyncio.timeout(45):
            client = SafeResearchHttpClient(
                allowed_hosts=_ALLOWED_HOSTS,
                allowed_content_types={"application/json"},
                proxy_url=self._proxy_url,
            )
            try:
                query = urlencode({
                    "search": normalized_topic[:300],
                    "per_page": str(_MAX_RESULTS),
                })
                search_url = f"{_SEARCH_ENDPOINT}?{query}"
                search_body, search_type, search_time = await client.get(search_url)
                search_data = _decode_json(search_body, search_type)
                if not isinstance(search_data, list):
                    raise ResearchFetchError("NASA search response was invalid.")

                docs: list[ResearchDocument] = []
                seen: set[str] = set()
                for item in search_data[:_MAX_RESULTS]:
                    if not isinstance(item, dict):
                        continue
                    links = item.get("_links")
                    self_links = links.get("self") if isinstance(links, dict) else None
                    href = None
                    if (
                        isinstance(self_links, list)
                        and self_links
                        and isinstance(self_links[0], dict)
                    ):
                        href = self_links[0].get("href")
                    detail_url = _safe_nasa_url(href)
                    canonical = _safe_nasa_url(item.get("url"))
                    if not detail_url or not canonical or canonical in seen:
                        continue

                    body, content_type, retrieved_at = await client.get(detail_url)
                    record = _decode_json(body, content_type)
                    if not isinstance(record, dict):
                        continue
                    title_data = record.get("title")
                    title = (
                        _plain_text(title_data.get("rendered"))
                        if isinstance(title_data, dict)
                        else ""
                    )
                    content_data = record.get("content")
                    rendered_content = (
                        content_data.get("rendered") if isinstance(content_data, dict) else None
                    )
                    content = (
                        _plain_text(rendered_content)
                        if isinstance(rendered_content, str)
                        else ""
                    )
                    if not title or len(content) < _MIN_EVIDENCE_CHARS:
                        continue
                    date_value = record.get("modified_gmt") or record.get("date_gmt")
                    published_at = None
                    if isinstance(date_value, str):
                        try:
                            published_at = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
                            if published_at.tzinfo is None:
                                published_at = published_at.replace(tzinfo=UTC)
                        except ValueError:
                            pass

                    docs.append(ResearchDocument(
                        canonical_url=canonical,
                        title=title[:500],
                        publisher="NASA",
                        source_type="webpage",
                        published_at=published_at,
                        retrieved_at=retrieved_at or search_time,
                        content=content,
                        evidence=(EvidenceExcerpt(
                            excerpt=_relevant_evidence(rendered_content, normalized_topic),
                            locator="NASA.gov article text (topic-relevant excerpt)",
                        ),),
                        rights_metadata=RightsMetadata(
                            status=RightsStatus.REVIEW_REQUIRED,
                            scope=RightsScope.SOURCE_TEXT,
                            creators=("NASA",),
                            attribution_text=f"{title[:500]} — NASA ({canonical})",
                            reuse_restrictions=(ReuseRestriction.NO_VERBATIM_REUSE_WITHOUT_LICENSE,),
                        ),
                    ))
                    seen.add(canonical)
                return tuple(docs)
            finally:
                await client.close()
