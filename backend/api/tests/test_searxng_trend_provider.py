import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.research.providers import searxng


class FakeClient:
    def __init__(self, *, allowed_hosts):
        self.allowed_hosts = allowed_hosts
        self.closed = False
        self.url = None

    async def get(self, url):
        self.url = url
        payload = {
            "results": [
                {
                    "title": "Astronomy observatory publishes new telescope image",
                    "url": "https://science.example.org/observatory/image",
                    "publishedDate": 1794484800,
                },
                {
                    "title": "New battery chemistry reported by researchers",
                    "url": "https://energy.example.org/research/battery",
                    "publishedDate": "2026-11-11T10:00:00Z",
                },
                {
                    "title": "Undated result must not be treated as fresh",
                    "url": "https://example.net/undated",
                },
                {
                    "title": "Unsafe result",
                    "url": "http://127.0.0.1/admin",
                    "publishedDate": 1794484800,
                },
            ]
        }
        return json.dumps(payload).encode(), "application/json", datetime(2026, 11, 12, tzinfo=UTC)

    async def close(self):
        self.closed = True


def test_searxng_provider_maps_bounded_results_to_discovery_observations(monkeypatch):
    clients = []

    def client_factory(*, allowed_hosts):
        client = FakeClient(allowed_hosts=allowed_hosts)
        clients.append(client)
        return client

    monkeypatch.setattr(searxng, "SafeResearchHttpClient", client_factory)
    provider = searxng.SearXNGTrendProvider(
        base_url="https://search.example.com",
        query="science technology discoveries",
    )
    result = asyncio.run(provider.discover(limit=10))

    assert len(result) == 2
    assert result[0].provider == "searxng"
    assert result[0].topic == "Astronomy observatory publishes new telescope image"
    assert result[0].momentum_score == 1.0
    assert result[0].metric_name == "searxng_result_rank"
    assert result[0].source_url == "https://science.example.org/observatory/image"
    assert result[0].publisher == "science.example.org"
    assert result[1].momentum_score == 0.0
    assert clients[0].allowed_hosts == frozenset({"search.example.com"})
    assert "format=json" in clients[0].url
    assert "q=science+technology+discoveries" in clients[0].url
    assert clients[0].closed is True


def test_searxng_provider_rejects_invalid_configuration():
    with pytest.raises(ValueError, match="HTTPS"):
        searxng.SearXNGTrendProvider(
            base_url="http://search.example.com",
            query="science",
        )
    with pytest.raises(ValueError, match="query"):
        searxng.SearXNGTrendProvider(
            base_url="https://search.example.com",
            query=" ",
        )


@pytest.mark.parametrize("limit", [0, 31, -1])
def test_searxng_provider_rejects_unbounded_limits(limit):
    provider = searxng.SearXNGTrendProvider(
        base_url="https://search.example.com",
        query="science",
    )
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(provider.discover(limit=limit))


def test_searxng_decoder_rejects_invalid_payloads():
    with pytest.raises(searxng.ResearchFetchError):
        searxng._decode_json(b"<html/>", "text/html")
    with pytest.raises(searxng.ResearchFetchError):
        searxng._decode_json(b"{bad", "application/json")


def test_searxng_settings_require_explicit_endpoint_and_query():
    with pytest.raises(ValueError, match="TREND_DISCOVERY_SEARXNG_BASE_URL"):
        Settings(_env_file=None, trend_discovery_provider="searxng")

    settings = Settings(
        _env_file=None,
        trend_discovery_provider="searxng",
        trend_discovery_searxng_base_url="https://search.example.com",
        trend_discovery_searxng_query="science technology discoveries",
    )
    assert settings.trend_discovery_provider == "searxng"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://search.example.com",
        "https://user:pass@search.example.com",
        "https://search.example.com?format=json",
        "https://search.example.com#fragment",
        "https://127.0.0.1",
    ],
)
def test_searxng_settings_reject_unsafe_endpoint(base_url):
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            trend_discovery_provider="searxng",
            trend_discovery_searxng_base_url=base_url,
        )


def test_production_searxng_settings_require_https():
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@example.com",
            email_outbox_encryption_key="b" * 64,
            trend_discovery_provider="searxng",
            trend_discovery_searxng_base_url="http://search.example.com",
        )



def test_dispatcher_selects_configured_searxng_provider_once(monkeypatch):
    from app.services import channel_schedule_dispatcher

    observed = (
        searxng.TrendObservation(
            provider="searxng",
            topic="New astronomy telescope discovery",
            observed_at=datetime(2026, 11, 12, 11, tzinfo=UTC),
            retrieved_at=datetime(2026, 11, 12, 12, tzinfo=UTC),
            momentum_score=1.0,
            source_url="https://science.example.org/telescope",
            publisher="science.example.org",
        ),
    )
    calls = []

    class FakeProvider:
        def __init__(self, *, base_url, query):
            calls.append((base_url, query))

        async def discover(self, *, limit):
            assert limit == 30
            return observed

    monkeypatch.setattr(channel_schedule_dispatcher, "SearXNGTrendProvider", FakeProvider)
    settings = Settings(
        _env_file=None,
        trend_discovery_provider="searxng",
        trend_discovery_searxng_base_url="https://search.example.com",
        trend_discovery_searxng_query="astronomy discoveries",
    )

    result = asyncio.run(channel_schedule_dispatcher._discover_trend_observations(settings))

    assert result == observed
    assert calls == [("https://search.example.com", "astronomy discoveries")]
