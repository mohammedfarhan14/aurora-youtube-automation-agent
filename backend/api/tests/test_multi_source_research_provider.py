import asyncio
from datetime import UTC, datetime

import pytest

from app.research.contracts import EvidenceExcerpt, ResearchDocument
from app.research.multi_source import MultiSourceResearchProvider


def make_document(url, content):
    return ResearchDocument(
        canonical_url=url,
        title="Research source",
        publisher="Example publisher",
        source_type="paper",
        published_at=None,
        retrieved_at=datetime(2026, 11, 12, tzinfo=UTC),
        content=content,
        evidence=(EvidenceExcerpt(excerpt=content),),
    )


class FakeProvider:
    def __init__(self, documents=(), error=None):
        self.documents = tuple(documents)
        self.error = error
        self.topics = []

    async def research(self, topic):
        self.topics.append(topic)
        if self.error is not None:
            raise self.error
        return self.documents


def test_multi_source_provider_queries_all_sources_concurrently_and_deduplicates():
    first = FakeProvider((make_document("https://example.org/paper", "abstract"),))
    second = FakeProvider((
        make_document("https://example.org/paper", "abstract"),
        make_document("https://example.org/other", "another abstract"),
    ))

    result = asyncio.run(
        MultiSourceResearchProvider((first, second)).research("  AI safety  ")
    )

    assert len(result) == 2
    assert first.topics == ["AI safety"]
    assert second.topics == ["AI safety"]


def test_multi_source_provider_rejects_conflicting_content_for_same_url():
    first = FakeProvider((make_document("https://example.org/paper", "first"),))
    second = FakeProvider((make_document("https://example.org/paper", "changed"),))

    with pytest.raises(ValueError, match="conflicting content"):
        asyncio.run(MultiSourceResearchProvider((first, second)).research("topic"))


def test_multi_source_provider_does_not_silently_ignore_provider_failure():
    first = FakeProvider((make_document("https://example.org/paper", "abstract"),))
    second = FakeProvider(error=RuntimeError("upstream unavailable"))

    with pytest.raises(RuntimeError, match="configured research provider failed"):
        asyncio.run(MultiSourceResearchProvider((first, second)).research("topic"))


def test_multi_source_provider_allows_empty_provider_results():
    first = FakeProvider((make_document("https://example.org/paper", "abstract"),))
    second = FakeProvider()

    result = asyncio.run(MultiSourceResearchProvider((first, second)).research("topic"))
    assert len(result) == 1


def test_multi_source_provider_requires_bounded_provider_count():
    with pytest.raises(ValueError, match="between 2 and"):
        MultiSourceResearchProvider((FakeProvider(),))
    with pytest.raises(ValueError, match="between 2 and"):
        MultiSourceResearchProvider(tuple(FakeProvider() for _ in range(6)))


def test_multi_source_provider_rejects_empty_topic():
    with pytest.raises(ValueError, match="must not be empty"):
        asyncio.run(
            MultiSourceResearchProvider((FakeProvider(), FakeProvider())).research(" ")
        )
