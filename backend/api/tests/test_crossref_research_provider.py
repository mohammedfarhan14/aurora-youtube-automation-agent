import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.research.providers import crossref


class FakeClient:
    def __init__(self, *, allowed_hosts):
        self.allowed_hosts = allowed_hosts
        self.calls = []
        self.closed = False

    async def get(self, url):
        self.calls.append(url)
        payload = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1234/example.paper",
                        "title": ["A useful scholarly paper"],
                        "publisher": "Example Academic Press",
                        "author": [
                            {"given": "Ada", "family": "Lovelace"},
                            {"name": "Example Research Group"},
                        ],
                        "abstract": (
                            "<jats:p>This study evaluates a carefully described "
                            "method and reports measurable results across several "
                            "independent experimental conditions.</jats:p>"
                        ),
                        "published-online": {"date-parts": [[2025, 4, 12]]},
                    },
                    {
                        "DOI": "10.1234/title-only",
                        "title": ["A title without an abstract"],
                    },
                ]
            }
        }
        return (
            json.dumps(payload).encode(),
            "application/json",
            datetime(2026, 11, 12, 12, tzinfo=UTC),
        )

    async def close(self):
        self.closed = True


def test_crossref_provider_returns_abstract_evidence_and_never_fetches_doi(monkeypatch):
    clients = []

    def client_factory(*, allowed_hosts):
        client = FakeClient(allowed_hosts=allowed_hosts)
        clients.append(client)
        return client

    monkeypatch.setattr(crossref, "SafeResearchHttpClient", client_factory)
    documents = asyncio.run(
        crossref.CrossrefResearchProvider().research("machine learning evaluation")
    )

    assert len(documents) == 1
    document = documents[0]
    assert document.canonical_url == "https://doi.org/10.1234/example.paper"
    assert document.title == "A useful scholarly paper"
    assert document.publisher == "Example Academic Press"
    assert document.source_type == "paper"
    assert document.published_at == datetime(2025, 4, 12, tzinfo=UTC)
    assert document.evidence[0].excerpt == document.content
    assert document.rights_metadata.status.value == "review_required"
    assert document.rights_metadata.scope.value == "abstract"
    assert document.rights_metadata.creators == ("Ada Lovelace", "Example Research Group")
    assert document.rights_metadata.license_ids == ()
    assert "no_verbatim_reuse_without_license" in {
        item.value for item in document.rights_metadata.reuse_restrictions
    }
    assert "This study evaluates" in document.content
    assert "<jats:p>" not in document.content
    assert clients[0].allowed_hosts == frozenset({"api.crossref.org"})
    assert len(clients[0].calls) == 1
    assert "query.bibliographic=machine+learning+evaluation" in clients[0].calls[0]
    assert clients[0].closed is True


def test_crossref_provider_rejects_empty_topic():
    with pytest.raises(ValueError, match="must not be empty"):
        asyncio.run(crossref.CrossrefResearchProvider().research("  "))


def test_crossref_response_rejects_invalid_payloads():
    with pytest.raises(crossref.ResearchFetchError):
        crossref._decode_response(b"<html/>", "text/html")
    with pytest.raises(crossref.ResearchFetchError):
        crossref._decode_response(b"{bad", "application/json")
    with pytest.raises(crossref.ResearchFetchError):
        crossref._decode_response(
            json.dumps({"message": {"items": "not-a-list"}}).encode(),
            "application/json",
        )


def test_crossref_abstract_stripping_ignores_script_and_style_content():
    result = crossref._plain_abstract(
        "<p>Useful <b>abstract</b> text</p><script>ignore this</script>"
        "<style>ignore this too</style>"
    )
    assert result == "Useful abstract text"


def test_crossref_publication_date_rejects_invalid_calendar_dates():
    assert crossref._published_at({
        "published-online": {"date-parts": [[2025, 13, 40]]}
    }) is None


def test_crossref_doi_validation_rejects_invalid_values():
    class InvalidDoiClient(FakeClient):
        async def get(self, url):
            self.calls.append(url)
            payload = {"message": {"items": [{
                "DOI": "javascript:alert(1)",
                "title": ["Untrusted DOI"],
                "abstract": "A substantive abstract with enough text to pass the filter. " * 3,
            }]}}
            return json.dumps(payload).encode(), "application/json", datetime.now(UTC)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        crossref, "SafeResearchHttpClient",
        lambda *, allowed_hosts: InvalidDoiClient(allowed_hosts=allowed_hosts),
    )
    try:
        assert asyncio.run(crossref.CrossrefResearchProvider().research("test")) == ()
    finally:
        monkeypatch.undo()
