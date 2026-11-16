"""Live research provider backed by the public MediaWiki search API.

The provider is opt-in. Every request, including redirects and URLs returned
by the remote API, passes through the SSRF-safe HTTP client.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import urlencode

import aiohttp

from app.research.contracts import EvidenceExcerpt, ResearchDocument
from app.research.normalization import normalize_source_url
from app.research.safe_http import (
    PublicOnlyResolver,
    ResearchFetchError,
    redirect_target,
    validate_research_proxy_url,
    validate_research_url,
)
from app.rights.contracts import (
    ReuseRestriction,
    RightsMetadata,
    RightsScope,
    RightsStatus,
)

_SEARCH_ENDPOINT = "https://en.wikipedia.org/w/api.php"
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_CONTENT_CHARS = 50_000
_MAX_RESULTS = 3
_MAX_SEARCH_RESULTS = 12
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ALLOWED_HOSTS = {"en.wikipedia.org"}
_TOPIC_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "latest", "of", "on", "or", "the", "to",
    "what", "when", "where", "why", "with",
})


def _topic_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token.casefold()
        for token in re.findall(r"[\w]+", value, flags=re.UNICODE)
        if len(token) > 1 and token.casefold() not in _TOPIC_STOPWORDS
    )


def _focused_search_query(value: str) -> str:
    terms = [
        token
        for token in re.findall(r"[\w]+", value, flags=re.UNICODE)
        if len(token) > 1 and token.casefold() not in _TOPIC_STOPWORDS
    ]
    return " ".join(terms)[:300] or value[:300]


def _search_relevance(topic_tokens: frozenset[str], title: str) -> float:
    """Score title match so broad search terms do not crowd out the subject."""
    title_tokens = _topic_tokens(title)
    if not topic_tokens or not title_tokens:
        return 0.0
    overlap = topic_tokens & title_tokens
    minimum_overlap = min(2, len(topic_tokens))
    if len(overlap) < minimum_overlap:
        return 0.0
    # Prefer titles that are mostly about the requested subject; use topic
    # coverage as a tie-breaker when titles contain additional words.
    return (len(overlap) / len(title_tokens)) + (len(overlap) / len(topic_tokens))


class SafeResearchHttpClient:
    """Small HTTP client with pinned validated DNS answers and bounded bodies."""

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | frozenset[str] = frozenset(_ALLOWED_HOSTS),
        allowed_content_types: set[str] | frozenset[str] = frozenset(
            {"application/json", "text/plain"}
        ),
        proxy_url: str | None = None,
    ) -> None:
        normalized_hosts = frozenset(host.strip().lower() for host in allowed_hosts)
        if not normalized_hosts or any(
            not host or "/" in host for host in normalized_hosts
        ):
            raise ValueError("At least one exact research host must be allowlisted.")
        self._allowed_hosts = normalized_hosts
        self._proxy_url = validate_research_proxy_url(proxy_url)
        self._allowed_content_types = frozenset(
            content_type.strip().lower()
            for content_type in allowed_content_types
        )
        if not self._allowed_content_types or any(
            not content_type or "/" not in content_type
            for content_type in self._allowed_content_types
        ):
            raise ValueError("At least one valid research content type is required.")
        self._connector = aiohttp.TCPConnector(
            resolver=PublicOnlyResolver() if self._proxy_url is None else None,
            use_dns_cache=False,
            ttl_dns_cache=0,
            limit=4,
            limit_per_host=2,
            enable_cleanup_closed=True,
        )
        self._timeout = aiohttp.ClientTimeout(
            total=12,
            connect=4,
            sock_connect=4,
            sock_read=5,
        )
        self._session = aiohttp.ClientSession(
            connector=self._connector,
            timeout=self._timeout,
            trust_env=False,
            auto_decompress=False,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": (
                    "AuroraStudioResearch/0.1 "
                    "(+https://github.com/mohammedfarhan14/aurora-youtube-automation-agent)"
                ),
            },
        )

    async def close(self) -> None:
        await self._session.close()

    async def get(self, url: str) -> tuple[bytes, str, datetime]:
        current = url
        for redirect_count in range(4):
            parsed = validate_research_url(current)
            if parsed.hostname not in self._allowed_hosts:
                raise ResearchFetchError("Research source host is not approved.")
            current = parsed.geturl()
            try:
                async with self._session.get(
                    current,
                    allow_redirects=False,
                    headers={"Host": parsed.netloc},
                    proxy=self._proxy_url,
                ) as response:
                    if response.status in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        current = redirect_target(current, location or "", redirect_count)
                        continue

                    if response.status < 200 or response.status >= 300:
                        raise ResearchFetchError("Research source returned an unsuccessful status.")

                    content_type = (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type not in self._allowed_content_types:
                        raise ResearchFetchError(
                            "Research source returned an unsupported content type."
                        )

                    content_encoding = response.headers.get("Content-Encoding", "identity").lower()
                    if content_encoding not in {"", "identity"}:
                        raise ResearchFetchError("Compressed research responses are not supported.")

                    declared_length = response.headers.get("Content-Length")
                    if declared_length is not None:
                        try:
                            if int(declared_length) > _MAX_RESPONSE_BYTES:
                                raise ResearchFetchError(
                                    "Research response exceeded the size limit."
                                )
                        except ValueError as exc:
                            raise ResearchFetchError(
                                "Research response length is invalid."
                            ) from exc

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(16_384):
                        total += len(chunk)
                        if total > _MAX_RESPONSE_BYTES:
                            raise ResearchFetchError("Research response exceeded the size limit.")
                        chunks.append(chunk)

                    retrieved_at = datetime.now(UTC)
                    return b"".join(chunks), content_type, retrieved_at
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                # Never propagate exception text: it can contain sensitive URLs.
                raise ResearchFetchError("Research source could not be retrieved safely.") from exc

        raise ResearchFetchError("Research source exceeded the redirect limit.")


class HttpResearchProvider:
    """Retrieve up to three public Wikipedia articles for a research topic."""

    def __init__(self, proxy_url: str | None = None) -> None:
        self._proxy_url = proxy_url

    async def research(self, topic: str) -> tuple[ResearchDocument, ...]:
        normalized_topic = topic.strip()
        if not normalized_topic:
            raise ValueError("Research topic must not be empty.")

        documents: list[ResearchDocument] = []
        topic_tokens = _topic_tokens(normalized_topic)
        search_query = _focused_search_query(normalized_topic)
        async with asyncio.timeout(45):
            client_options = (
                {"proxy_url": self._proxy_url}
                if self._proxy_url is not None
                else {}
            )
            client = SafeResearchHttpClient(**client_options)
            try:
                search_url = f"{_SEARCH_ENDPOINT}?{urlencode({
                    'action': 'query',
                    'list': 'search',
                    'srsearch': search_query,
                    'srlimit': str(_MAX_SEARCH_RESULTS),
                    'format': 'json',
                    'utf8': '1',
                })}"
                search_body, content_type, _ = await client.get(search_url)
                search_data = _decode_json(search_body, content_type)
                results = search_data.get("query", {}).get("search", [])
                if not isinstance(results, list):
                    raise ResearchFetchError("Research search response was invalid.")

                ranked_results: list[tuple[float, int, str]] = []
                for item in results[:_MAX_SEARCH_RESULTS]:
                    if not isinstance(item, dict):
                        continue
                    page_id = item.get("pageid")
                    title = item.get("title")
                    if not isinstance(page_id, int) or not isinstance(title, str):
                        continue
                    score = _search_relevance(topic_tokens, title)
                    if score > 0:
                        ranked_results.append((score, page_id, title))

                ranked_results.sort(key=lambda result: result[0], reverse=True)
                for _, page_id, title in ranked_results[:_MAX_RESULTS]:

                    page_url = f"{_SEARCH_ENDPOINT}?{urlencode({
                        'action': 'query',
                        'prop': 'extracts|info|revisions',
                        'explaintext': '1',
                        'exsectionformat': 'plain',
                        'inprop': 'url',
                        'rvprop': 'ids|timestamp',
                        'rvlimit': '1',
                        'pageids': str(page_id),
                        'format': 'json',
                        'utf8': '1',
                    })}"
                    page_body, page_type, retrieved_at = await client.get(page_url)
                    page_data = _decode_json(page_body, page_type)
                    pages = page_data.get("query", {}).get("pages", {})
                    page = pages.get(str(page_id)) if isinstance(pages, dict) else None
                    if not isinstance(page, dict):
                        continue

                    content = page.get("extract")
                    canonical = page.get("fullurl")
                    page_title = page.get("title", title)
                    revisions = page.get("revisions")
                    revision = revisions[0] if isinstance(revisions, list) and revisions else None
                    revision_id = revision.get("revid") if isinstance(revision, dict) else None
                    if (
                        not isinstance(content, str)
                        or not content.strip()
                        or not isinstance(revision_id, int)
                        or isinstance(revision_id, bool)
                        or revision_id <= 0
                    ):
                        # A page without a stable revision cannot carry auditable
                        # source/license provenance, so it is not accepted.
                        continue
                    if not isinstance(canonical, str):
                        canonical = f"https://en.wikipedia.org/wiki/{page_id}"
                    # Validate metadata URLs too; never trust a remote API URL.
                    canonical_parts = validate_research_url(canonical)
                    if canonical_parts.hostname not in _ALLOWED_HOSTS:
                        continue
                    canonical = normalize_source_url(canonical_parts.geturl())
                    revision_url = f"https://en.wikipedia.org/w/index.php?oldid={revision_id}"
                    revision_parts = validate_research_url(revision_url)
                    if revision_parts.hostname not in _ALLOWED_HOSTS:
                        continue
                    revision_url = revision_parts.geturl()
                    normalized_title = unescape(str(page_title))[:500]
                    content = content.strip()[:_MAX_CONTENT_CHARS]
                    excerpt = content[:1200].strip()
                    if not excerpt:
                        continue

                    rights_metadata = RightsMetadata(
                        status=RightsStatus.REVIEW_REQUIRED,
                        scope=RightsScope.SOURCE_TEXT,
                        license_ids=("CC-BY-SA-4.0",),
                        license_urls=("https://creativecommons.org/licenses/by-sa/4.0/",),
                        rights_holder="Wikipedia contributors",
                        creators=("Wikipedia contributors",),
                        source_revision_url=revision_url,
                        attribution_text=(
                            f'"{normalized_title}" — Wikipedia contributors '
                            f'(revision {revision_id}); CC BY-SA 4.0. '
                            "Review page history and discussion for additional notices."
                        ),
                        reuse_restrictions=(
                            ReuseRestriction.ATTRIBUTION,
                            ReuseRestriction.SHARE_ALIKE,
                            ReuseRestriction.INDICATE_MODIFICATIONS,
                            ReuseRestriction.REVIEW_PAGE_HISTORY,
                        ),
                    )
                    documents.append(
                        ResearchDocument(
                            canonical_url=canonical,
                            title=normalized_title,
                            publisher="Wikipedia",
                            source_type="webpage",
                            published_at=None,
                            retrieved_at=retrieved_at,
                            content=content,
                            evidence=(EvidenceExcerpt(
                                excerpt=excerpt,
                                locator=f"Wikipedia page {page_id}, revision {revision_id}",
                            ),),
                            rights_metadata=rights_metadata,
                        )
                    )
            finally:
                await client.close()

        return tuple(documents)


def _decode_json(body: bytes, content_type: str) -> dict[str, Any]:
    if content_type != "application/json":
        raise ResearchFetchError("Research API returned an unsupported content type.")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchFetchError("Research API returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise ResearchFetchError("Research API returned an invalid document.")
    return value
