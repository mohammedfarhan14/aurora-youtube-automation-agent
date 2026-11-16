"""Opt-in SearXNG discovery for channel-relevant topic leads.

SearXNG results are trend/discovery observations only. Search snippets and
ranking metadata are deliberately not persisted as factual evidence.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from app.research.providers.http import SafeResearchHttpClient
from app.research.safe_http import ResearchFetchError, validate_research_url
from app.research.trend_ranking import TrendObservation

_MAX_RESULTS = 30
_MAX_QUERY_LENGTH = 300


class SearXNGTrendProvider:
    """Query one explicitly configured SearXNG instance for bounded search leads."""

    def __init__(self, *, base_url: str, query: str, timeout_seconds: int = 45) -> None:
        parsed = validate_research_url(base_url.strip())
        if parsed.scheme != "https":
            raise ValueError("SearXNG base URL must use HTTPS.")
        if parsed.query or parsed.fragment:
            raise ValueError("SearXNG base URL must not contain a query or fragment.")
        if not query.strip() or len(query.strip()) > _MAX_QUERY_LENGTH:
            raise ValueError("SearXNG query must contain 1 to 300 characters.")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("SearXNG timeout must be between 1 and 120 seconds.")
        self._base_url = parsed.geturl().rstrip("/")
        self._host = parsed.hostname
        self._query = query.strip()
        self._timeout_seconds = timeout_seconds

    async def discover(self, *, limit: int = 20) -> tuple[TrendObservation, ...]:
        if not 1 <= limit <= _MAX_RESULTS:
            raise ValueError(f"SearXNG trend limit must be between 1 and {_MAX_RESULTS}.")

        observations: list[TrendObservation] = []
        async with asyncio.timeout(self._timeout_seconds):
            client = SafeResearchHttpClient(allowed_hosts=frozenset({self._host}))
            try:
                params = urlencode({
                    "q": self._query,
                    "format": "json",
                    "categories": "general,news,science",
                    "language": "en",
                    "safesearch": "2",
                    "pageno": "1",
                })
                body, content_type, retrieved_at = await client.get(
                    f"{self._base_url}/search?{params}"
                )
                payload = _decode_json(body, content_type)
                results = payload.get("results")
                if not isinstance(results, list):
                    raise ResearchFetchError("SearXNG returned an invalid result collection.")

                candidates: list[dict[str, Any]] = []
                seen_urls: set[str] = set()
                for item in results[:limit]:
                    if not isinstance(item, dict):
                        continue
                    title = item.get("title")
                    raw_url = item.get("url")
                    published_at = _published_at(item.get("publishedDate"))
                    if (
                        not isinstance(title, str)
                        or not title.strip()
                        or len(title.strip()) > 500
                        or published_at is None
                        or not isinstance(raw_url, str)
                    ):
                        continue
                    try:
                        source = validate_research_url(raw_url)
                    except ValueError:
                        continue
                    if source.scheme != "https" or not source.hostname:
                        continue
                    canonical_url = source.geturl()
                    if canonical_url in seen_urls:
                        continue
                    seen_urls.add(canonical_url)
                    candidates.append({
                        "title": title.strip(),
                        "url": canonical_url,
                        "published_at": published_at,
                        "publisher": source.hostname[:200],
                    })

                for index, candidate in enumerate(candidates):
                    momentum = (
                        1.0
                        if len(candidates) == 1
                        else 1.0 - index / (len(candidates) - 1)
                    )
                    observations.append(
                        TrendObservation(
                            provider="searxng",
                            topic=candidate["title"],
                            observed_at=candidate["published_at"],
                            retrieved_at=retrieved_at,
                            momentum_score=momentum,
                            metric_name="searxng_result_rank",
                            metric_value=float(index + 1),
                            metric_unit="rank",
                            geography="global",
                            language="en",
                            source_url=candidate["url"],
                            publisher=candidate["publisher"],
                        )
                    )
            finally:
                await client.close()

        return tuple(observations)


def _published_at(value: object) -> datetime | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    return None


def _decode_json(body: bytes, content_type: str) -> dict[str, Any]:
    if content_type != "application/json":
        raise ResearchFetchError("SearXNG returned an unsupported content type.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchFetchError("SearXNG returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ResearchFetchError("SearXNG returned an invalid document.")
    return payload
