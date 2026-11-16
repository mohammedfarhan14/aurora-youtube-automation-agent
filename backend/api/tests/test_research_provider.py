import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.research.providers.mock import MockResearchProvider


def test_mock_provider_returns_deterministic_sources():
    provider = MockResearchProvider()
    topic = "Renewable energy storage"

    first_result = asyncio.run(provider.research(topic))
    second_result = asyncio.run(provider.research(topic))

    assert [item.canonical_url for item in first_result] == [
        item.canonical_url for item in second_result
    ]
    assert len(first_result) == 2

    retrieval_started_at = datetime.now(UTC) - timedelta(seconds=5)
    for document in first_result:
        assert document.retrieved_at.tzinfo is not None
        assert document.retrieved_at >= retrieval_started_at
        assert document.canonical_url.startswith(
            "https://example.com/mock-research/"
        )
        assert document.title
        assert document.publisher == "Aurora Studio Mock Provider"
        assert document.source_type == "other"
        assert document.content
        assert document.evidence
        assert all(item.excerpt for item in document.evidence)

        assert "Synthetic" in document.content
        assert "not a real-world source" in document.content or (
            "must not be presented as verified research" in document.content
        )


def test_mock_provider_returns_different_sources_for_different_topics():
    provider = MockResearchProvider()

    first = asyncio.run(provider.research("Renewable energy"))
    second = asyncio.run(provider.research("Electric vehicles"))

    assert first != second
    assert first[0].canonical_url != second[0].canonical_url


def test_mock_provider_rejects_blank_topic():
    provider = MockResearchProvider()

    with pytest.raises(ValueError, match="must not be empty"):
        asyncio.run(provider.research("   "))
