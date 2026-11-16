import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.research.providers.searxng_research import SearXNGResearchProvider
from app.rights.contracts import ReuseRestriction, RightsScope, RightsStatus


def provider():
    return SearXNGResearchProvider(base_url="https://search.example.org")


def test_search_candidates_are_ranked_and_restricted_to_relevant_https_sources():
    candidates = provider()._candidates({"results": [
        {
            "title": "NASA",
            "url": "https://nasa.gov/about",
            "content": "NASA is an independent agency.",
        },
        {
            "title": "Artemis II mission schedule",
            "url": "https://nasa.gov/artemis-ii?utm_source=search",
            "content": "Flight information and schedule.",
        },
        {
            "title": "Artemis II mission schedule",
            "url": "http://nasa.gov/artemis-ii",
            "content": "Insecure candidate is excluded.",
        },
        {
            "title": "Artemis II mission schedule",
            "url": "https://search.example.org/search?q=loop",
            "content": "Search endpoint is excluded.",
        },
        {
            "title": "NASA",
            "url": "https://example.com/private-address",
            "content": "Not sufficiently relevant.",
        },
    ]}, "NASA Artemis II schedule")

    assert len(candidates) == 1
    assert candidates[0]["url"] == "https://nasa.gov/artemis-ii?utm_source=search"


def test_fetched_page_becomes_review_required_evidence_and_filters_active_markup(monkeypatch):
    retrieved_at = datetime(2026, 11, 15, tzinfo=UTC)
    page = (
        b"<html><head><title>Artemis II mission schedule</title></head><body>"
        b"<nav>Navigation is not evidence.</nav><main><h1>Artemis II</h1>"
        b"<p>NASA Artemis II is the first crewed mission of the Artemis program. "
        b"This paragraph contains enough detail to be retained as source evidence.</p>"
        b"<p>The mission is planned to test spacecraft systems around the Moon. "
        b"This second paragraph has enough additional context for the research run.</p>"
        b"<div aria-hidden='true'>Hidden sidebar text.</div>"
        b"<p>Visible material after hidden content remains available for review. "
        b"This confirms the parser resumes after a hidden element closes.</p>"
        b"<script>ignoreThisSecretLikeText()</script></main></body></html>"
    )

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["allowed_hosts"] == frozenset({"nasa.gov"})
            assert "text/html" in kwargs["allowed_content_types"]
            self.closed = False

        async def get(self, url):
            assert url == "https://nasa.gov/artemis-ii"
            return page, "text/html", retrieved_at

        async def close(self):
            self.closed = True

    clients = []

    def client_factory(**kwargs):
        client = FakeClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(
        "app.research.providers.searxng_research.SafeResearchHttpClient",
        client_factory,
    )
    result = asyncio.run(provider()._fetch_candidate({
        "title": "Search result title",
        "url": "https://nasa.gov/artemis-ii",
        "host": "nasa.gov",
    }, retrieved_at))

    assert result is not None
    assert result.title == "Artemis II mission schedule"
    assert "Navigation is not evidence" not in result.content
    assert "ignoreThisSecretLikeText" not in result.content
    assert "Hidden sidebar text" not in result.content
    assert "Visible material after hidden content" in result.content
    assert result.evidence
    assert result.rights_metadata.status == RightsStatus.REVIEW_REQUIRED
    assert result.rights_metadata.scope == RightsScope.SOURCE_TEXT
    assert result.rights_metadata.reuse_restrictions == (
        ReuseRestriction.NO_VERBATIM_REUSE_WITHOUT_LICENSE,
    )
    assert clients[0].closed is True


def test_provider_searches_then_fetches_article_without_using_snippet_as_evidence(monkeypatch):
    retrieved_at = datetime(2026, 11, 15, tzinfo=UTC)
    long_article = (
        "NASA Artemis II will test the spacecraft and crew systems during a lunar flyby. "
        "The mission is part of a broader human exploration program. "
    ) * 8
    opened_hosts = []

    class FakeClient:
        def __init__(self, *, allowed_hosts, allowed_content_types=None, proxy_url):
            self.hosts = allowed_hosts
            self.closed = False
            opened_hosts.append(allowed_hosts)

        async def get(self, url):
            if "search.example.org" in self.hosts:
                payload = {"results": [{
                    "title": "Artemis II mission schedule",
                    "url": "https://nasa.gov/artemis-ii",
                    "content": "Search snippet must not appear in the evidence body.",
                }]}
                return json.dumps(payload).encode(), "application/json", retrieved_at
            html = (
                "<html><head><title>NASA Artemis II</title></head><body><main><p>"
                + long_article
                + "</p></main></body></html>"
            ).encode()
            return html, "text/html", retrieved_at

        async def close(self):
            self.closed = True

    clients = []

    def client_factory(**kwargs):
        client = FakeClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(
        "app.research.providers.searxng_research.SafeResearchHttpClient",
        client_factory,
    )
    documents = asyncio.run(provider().research("NASA Artemis II schedule"))

    assert len(documents) == 1
    assert documents[0].publisher == "nasa.gov"
    assert documents[0].content == long_article.strip()
    assert "Search snippet" not in documents[0].content
    assert opened_hosts == [frozenset({"search.example.org"}), frozenset({"nasa.gov"})]
    assert all(client.closed for client in clients)


@pytest.mark.parametrize("url", [
    "http://search.example.org",
    "https://127.0.0.1",
    "https://user:pass@search.example.org",
    "https://search.example.org?q=unsafe",
])
def test_provider_requires_valid_https_search_origin(url):
    with pytest.raises(ValueError):
        SearXNGResearchProvider(base_url=url)

