import asyncio
import json
from datetime import UTC, datetime

from app.research.providers.nasa import NasaResearchProvider, _safe_nasa_url
from app.rights.contracts import RightsStatus


class _ClientStub:
    def __init__(self, **kwargs):
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        if "wp-json/wp/v2/search" in url:
            payload = [{
                "id": 1,
                "title": "Gateway FAQ",
                "url": "https://www.nasa.gov/gateway-frequently-asked-questions/",
                "_links": {"self": [{"href": "https://www.nasa.gov/wp-json/wp/v2/topic/1"}]},
            }]
        else:
            payload = {
                "title": {"rendered": "Gateway FAQ"},
                "content": {
                    "rendered": (
                        "<p>NASA says Gateway supports long duration exploration. "
                        "The lunar outpost supports scientific research, visiting spacecraft, "
                        "and long-term exploration missions in orbit around the Moon.</p>"
                        "<script>ignore()</script>"
                    )
                },
                "modified_gmt": "2026-07-23T15:07:04",
            }
        return json.dumps(payload).encode(), "application/json", datetime.now(UTC)

    async def close(self):
        return None


def test_nasa_provider_returns_bounded_evidence_and_requires_rights_review(monkeypatch):
    monkeypatch.setattr("app.research.providers.nasa.SafeResearchHttpClient", _ClientStub)
    provider = NasaResearchProvider(proxy_url="http://research-egress-proxy:3128")

    documents = asyncio.run(provider.research("NASA Gateway long duration missions"))

    assert len(documents) == 1
    document = documents[0]
    assert document.publisher == "NASA"
    assert document.canonical_url == "https://www.nasa.gov/gateway-frequently-asked-questions/"
    assert "supports long duration exploration" in document.content
    assert "ignore()" not in document.content
    assert document.rights_metadata.status == RightsStatus.REVIEW_REQUIRED
    assert document.evidence[0].excerpt
    assert len(document.evidence[0].excerpt) <= 1_600
    assert "long-term exploration missions" in document.evidence[0].excerpt


def test_nasa_provider_rejects_unapproved_hosts_and_query_urls():
    assert _safe_nasa_url("https://www.nasa.gov/article/") == "https://www.nasa.gov/article/"
    assert _safe_nasa_url("https://evil.example/article/") is None
    assert _safe_nasa_url("https://www.nasa.gov/article/?next=https://evil.example") is None
