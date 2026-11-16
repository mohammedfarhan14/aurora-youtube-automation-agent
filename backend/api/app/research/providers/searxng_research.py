"""Factual source retrieval through an explicitly configured SearXNG instance.

Search snippets are used only to discover candidate URLs. Evidence is extracted
from the fetched HTTPS page itself, with unverified copyright status retained as
review-required metadata. Candidate hosts are exact-allowlisted per request and
all traffic uses Aurora's research egress route when configured.
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlencode

from app.research.contracts import EvidenceExcerpt, ResearchDocument
from app.research.normalization import normalize_source_url
from app.research.providers.http import SafeResearchHttpClient, _decode_json
from app.research.safe_http import ResearchFetchError, validate_research_url
from app.rights.contracts import (
    ReuseRestriction,
    RightsMetadata,
    RightsScope,
    RightsStatus,
)

_MAX_RESULTS = 8
_MAX_DOCUMENTS = 3
_MAX_PAGE_CHARS = 50_000
_MAX_QUERY_CHARS = 300
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_SUPPRESSED_TAGS = frozenset({
    "script", "style", "noscript", "svg", "canvas", "nav", "footer",
    "header", "aside", "form", "button", "template",
})
_BLOCK_TAGS = frozenset({"article", "main", "p", "div", "section", "li", "br", "h1", "h2", "h3"})
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "latest", "of", "on", "or", "the", "to",
    "what", "when", "where", "why", "with",
})


class _ArticleParser(HTMLParser):
    """Small bounded HTML text extractor that excludes active/UI elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._suppressed_tags: list[str] = []
        self._in_title = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attrs_map = {key.casefold(): (value or "").casefold() for key, value in attrs}
        class_text = f"{attrs_map.get('class', '')} {attrs_map.get('id', '')}"
        is_suppressed = tag in _SUPPRESSED_TAGS or attrs_map.get("aria-hidden") == "true" or any(
            marker in class_text for marker in ("cookie", "advert", "sidebar", "navigation")
        )
        if is_suppressed:
            self._suppressed_tags.append(tag)
        if tag == "title":
            self._in_title += 1
        if tag in _BLOCK_TAGS and not self._suppressed_tags:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title" and self._in_title:
            self._in_title -= 1
        if tag in self._suppressed_tags:
            index = len(self._suppressed_tags) - 1 - self._suppressed_tags[::-1].index(tag)
            self._suppressed_tags.pop(index)
        if tag in _BLOCK_TAGS and not self._suppressed_tags:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif not self._suppressed_tags:
            self.parts.append(data)

    def result(self) -> tuple[str, str]:
        title = " ".join(" ".join(self.title_parts).split())[:500]
        text = " ".join(" ".join(self.parts).split())[:_MAX_PAGE_CHARS]
        return title, text


class SearXNGResearchProvider:
    """Search, fetch, and retain a small set of source pages as research evidence."""

    def __init__(
        self,
        *,
        base_url: str,
        proxy_url: str | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        parsed = validate_research_url(base_url.strip())
        if parsed.scheme != "https" or parsed.query or parsed.fragment:
            raise ValueError("SearXNG research endpoint must be an HTTPS origin/path.")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("SearXNG research timeout must be between 1 and 120 seconds.")
        self._base_url = parsed.geturl().rstrip("/")
        self._host = parsed.hostname or ""
        self._proxy_url = proxy_url
        self._timeout_seconds = timeout_seconds

    async def research(self, topic: str) -> tuple[ResearchDocument, ...]:
        query = topic.strip()
        if not query or len(query) > _MAX_QUERY_CHARS:
            raise ValueError("Research topic must contain 1 to 300 characters.")

        async with asyncio.timeout(self._timeout_seconds):
            search_client = SafeResearchHttpClient(
                allowed_hosts=frozenset({self._host}),
                proxy_url=self._proxy_url,
            )
            try:
                params = urlencode({
                    "q": query,
                    "format": "json",
                    "categories": "general,news,science",
                    "language": "en",
                    "safesearch": "2",
                    "pageno": "1",
                })
                body, content_type, search_time = await search_client.get(
                    f"{self._base_url}/search?{params}"
                )
                payload = _decode_json(body, content_type)
            finally:
                await search_client.close()

            candidates = self._candidates(payload, query)
            results = await asyncio.gather(
                *(self._fetch_candidate(candidate, search_time) for candidate in candidates),
                return_exceptions=True,
            )
            documents = tuple(
                result for result in results
                if isinstance(result, ResearchDocument)
            )
            if not documents and candidates:
                raise ResearchFetchError("Search results could not be retrieved as source pages.")
            return documents

    def _candidates(self, payload: dict, topic: str) -> tuple[dict[str, str], ...]:
        results = payload.get("results")
        if not isinstance(results, list):
            raise ResearchFetchError("SearXNG returned an invalid result collection.")
        topic_terms = _tokens(topic)
        if not topic_terms:
            return ()
        candidates: list[tuple[float, dict[str, str]]] = []
        seen: set[str] = set()
        for item in results[:_MAX_RESULTS]:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            raw_url = item.get("url")
            if not isinstance(title, str) or not title.strip() or len(title) > 500:
                continue
            if not isinstance(raw_url, str):
                continue
            try:
                source = validate_research_url(raw_url)
            except ValueError:
                continue
            if source.scheme != "https" or not source.hostname:
                continue
            canonical = normalize_source_url(source.geturl())
            if canonical in seen or source.hostname == self._host:
                continue
            seen.add(canonical)
            snippet = item.get("content")
            snippet = snippet if isinstance(snippet, str) else ""
            title_terms = _tokens(title)
            matched = topic_terms & (title_terms | _tokens(snippet[:2000]))
            minimum_overlap = min(2, len(topic_terms))
            if len(matched) < minimum_overlap:
                continue
            score = 2 * len(topic_terms & title_terms) + len(matched) / max(1, len(topic_terms))
            candidates.append((score, {
                "title": title.strip(),
                "url": canonical,
                "host": source.hostname,
            }))
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        return tuple(candidate for _, candidate in candidates[:_MAX_DOCUMENTS])

    async def _fetch_candidate(
        self,
        candidate: dict[str, str],
        search_time: datetime,
    ) -> ResearchDocument | None:
        client = SafeResearchHttpClient(
            allowed_hosts=frozenset({candidate["host"]}),
            allowed_content_types=frozenset({"application/json", "text/plain", *_HTML_TYPES}),
            proxy_url=self._proxy_url,
        )
        try:
            body, content_type, retrieved_at = await client.get(candidate["url"])
        except ResearchFetchError:
            return None
        finally:
            await client.close()

        if content_type not in _HTML_TYPES:
            return None
        parser = _ArticleParser()
        try:
            parser.feed(body.decode("utf-8", errors="replace"))
            parser.close()
        except Exception:
            return None
        page_title, content = parser.result()
        if len(content) < 300:
            return None
        title = page_title or candidate["title"]
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", content)
            if len(sentence.strip()) >= 50
        ]
        evidence = tuple(EvidenceExcerpt(
            excerpt=sentence[:1200],
            locator=f"Fetched page, paragraph {index + 1}",
        ) for index, sentence in enumerate(sentences[:3]))
        if not evidence:
            return None

        return ResearchDocument(
            canonical_url=candidate["url"],
            title=title,
            publisher=candidate["host"],
            source_type="webpage",
            published_at=None,
            retrieved_at=retrieved_at or search_time or datetime.now(UTC),
            content=content,
            evidence=evidence,
            rights_metadata=RightsMetadata(
                status=RightsStatus.REVIEW_REQUIRED,
                scope=RightsScope.SOURCE_TEXT,
                attribution_text=(
                    f"{title} — {candidate['host']}. Retrieved {retrieved_at:%Y-%m-%d}. "
                    "Page license not verified; do not reuse text or media verbatim."
                ),
                reuse_restrictions=(ReuseRestriction.NO_VERBATIM_REUSE_WITHOUT_LICENSE,),
            ),
        )


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token.casefold()
        for token in re.findall(r"[\w]+", value, flags=re.UNICODE)
        if len(token) > 1 and token.casefold() not in _STOPWORDS
    )
