import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.research.providers import http as research_http
from app.research.safe_http import ResearchFetchError, UnsafeResearchTarget
from app.rights.contracts import RightsStatus


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(b'{"ok":true}',)):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self):
        self.closed = True


def make_client(monkeypatch, responses):
    session = FakeSession(responses)

    class FakeConnector:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(research_http.aiohttp, "TCPConnector", FakeConnector)
    monkeypatch.setattr(research_http.aiohttp, "ClientSession", lambda **kwargs: session)
    return research_http.SafeResearchHttpClient(), session


def test_client_disables_automatic_redirects_and_revalidates_target(monkeypatch):
    client, session = make_client(
        monkeypatch,
        [FakeResponse(status=302, headers={"Location": "http://127.0.0.1/admin"})],
    )

    with pytest.raises(ResearchFetchError, match="unsafe redirect"):
        asyncio.run(client.get("https://en.wikipedia.org/w/api.php"))

    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False


def test_client_rejects_redirect_to_non_allowlisted_public_host(monkeypatch):
    client, session = make_client(
        monkeypatch,
        [FakeResponse(status=302, headers={"Location": "https://example.com/"})],
    )

    async def run():
        with pytest.raises(ResearchFetchError, match="host is not approved"):
            await client.get("https://en.wikipedia.org/w/api.php")

    asyncio.run(run())
    assert len(session.calls) == 1


def test_client_rejects_unsupported_content_type(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        [FakeResponse(headers={"Content-Type": "text/html"})],
    )

    with pytest.raises(ResearchFetchError, match="unsupported content type"):
        asyncio.run(client.get("https://en.wikipedia.org/w/api.php"))


def test_client_aborts_stream_when_body_exceeds_limit(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        [FakeResponse(chunks=(b"x" * (research_http._MAX_RESPONSE_BYTES + 1),))],
    )

    with pytest.raises(ResearchFetchError, match="size limit"):
        asyncio.run(client.get("https://en.wikipedia.org/w/api.php"))


def test_client_rejects_compressed_response(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        [FakeResponse(headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        })],
    )

    with pytest.raises(ResearchFetchError, match="Compressed"):
        asyncio.run(client.get("https://en.wikipedia.org/w/api.php"))


def test_client_propagates_cancellation(monkeypatch):
    client, _ = make_client(monkeypatch, [asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client.get("https://en.wikipedia.org/w/api.php"))


def test_mediawiki_json_decoder_rejects_invalid_payloads():
    with pytest.raises(ResearchFetchError):
        research_http._decode_json(b"<html>no</html>", "text/html")
    with pytest.raises(ResearchFetchError):
        research_http._decode_json(b"{broken", "application/json")


def test_provider_rejects_empty_topic():
    with pytest.raises(ValueError, match="must not be empty"):
        asyncio.run(research_http.HttpResearchProvider().research("  "))


def test_search_ranking_prefers_subject_title_over_generic_term_overlap():
    topic = research_http._topic_tokens(
        "Latest NASA Artemis II launch schedule and mission plan"
    )

    subject_score = research_http._search_relevance(topic, "Artemis II")
    generic_score = research_http._search_relevance(topic, "NASA")
    loosely_related_score = research_http._search_relevance(
        topic, "List of Starship launches"
    )

    assert subject_score > generic_score
    assert subject_score > loosely_related_score
    assert generic_score == 0
    assert loosely_related_score == 0


def test_search_ranking_keeps_compact_topics_retrievable():
    topic = research_http._topic_tokens("solar energy")

    assert research_http._search_relevance(topic, "Solar energy") > 0
    assert research_http._search_relevance(topic, "Solar power") == 0


def test_focused_search_query_removes_generic_words_but_keeps_subject_terms():
    assert research_http._focused_search_query(
        "Latest NASA Artemis II launch schedule and mission plan"
    ) == "NASA Artemis II launch schedule mission plan"
    assert research_http._focused_search_query("why is it a") == "why is it a"


def test_client_never_attempts_a_private_ip_literal(monkeypatch):
    client, session = make_client(monkeypatch, [])

    with pytest.raises(UnsafeResearchTarget):
        asyncio.run(client.get("http://127.0.0.1/admin"))

    assert session.calls == []


def test_client_sanitizes_timeout_errors(monkeypatch):
    client, _ = make_client(monkeypatch, [TimeoutError("sensitive-upstream-url")])

    with pytest.raises(ResearchFetchError, match="could not be retrieved safely") as error:
        asyncio.run(client.get("https://en.wikipedia.org/w/api.php"))

    assert "sensitive-upstream-url" not in str(error.value)


def test_client_disables_dns_cache(monkeypatch):
    client, _ = make_client(monkeypatch, [])
    assert client._connector.kwargs["use_dns_cache"] is False
    assert client._connector.kwargs["ttl_dns_cache"] == 0


def test_client_rejects_unapproved_host_before_network(monkeypatch):
    client, session = make_client(monkeypatch, [])

    with pytest.raises(ResearchFetchError, match="host is not approved"):
        asyncio.run(client.get("https://example.com/"))

    assert session.calls == []


class FakeWikipediaProviderClient:
    def __init__(self, *, include_revision=True):
        self.include_revision = include_revision
        self.calls = []
        self.closed = False

    async def get(self, url):
        self.calls.append(url)
        if "list=search" in url:
            payload = {"query": {"search": [{"pageid": 123, "title": "Solar energy"}]}}
        else:
            page = {
                "extract": "Solar energy is radiant light and heat from the Sun.",
                "fullurl": "https://en.wikipedia.org/wiki/Solar_energy",
                "title": "Solar energy",
            }
            if self.include_revision:
                page["revisions"] = [{"revid": 456789, "timestamp": "2026-11-12T00:00:00Z"}]
            payload = {"query": {"pages": {"123": page}}}
        return (
            json.dumps(payload).encode(),
            "application/json",
            datetime(2026, 11, 13, tzinfo=UTC),
        )

    async def close(self):
        self.closed = True


def test_wikipedia_provider_records_revision_and_explicit_text_license(monkeypatch):
    clients = []

    def client_factory():
        client = FakeWikipediaProviderClient()
        clients.append(client)
        return client

    monkeypatch.setattr(research_http, "SafeResearchHttpClient", client_factory)
    documents = asyncio.run(research_http.HttpResearchProvider().research("solar energy"))

    assert len(documents) == 1
    document = documents[0]
    rights = document.rights_metadata
    assert rights.status == RightsStatus.REVIEW_REQUIRED
    assert rights.scope.value == "source_text"
    assert rights.license_ids == ("CC-BY-SA-4.0",)
    assert rights.license_urls == ("https://creativecommons.org/licenses/by-sa/4.0/",)
    assert rights.rights_holder == "Wikipedia contributors"
    assert rights.source_revision_url == (
        "https://en.wikipedia.org/w/index.php?oldid=456789"
    )
    assert "Solar energy" in rights.attribution_text
    assert "456789" in rights.attribution_text
    assert {item.value for item in rights.reuse_restrictions} == {
        "attribution",
        "share_alike",
        "indicate_modifications",
        "review_page_history",
    }
    assert "prop=extracts%7Cinfo%7Crevisions" in clients[0].calls[1]
    assert "rvprop=ids%7Ctimestamp" in clients[0].calls[1]
    assert clients[0].closed is True


def test_wikipedia_provider_rejects_page_without_stable_revision(monkeypatch):
    client = FakeWikipediaProviderClient(include_revision=False)
    monkeypatch.setattr(
        research_http,
        "SafeResearchHttpClient",
        lambda: client,
    )

    assert asyncio.run(research_http.HttpResearchProvider().research("solar energy")) == ()
    assert client.closed is True
