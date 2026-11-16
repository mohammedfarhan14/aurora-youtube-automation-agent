from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.research.contracts import EvidenceExcerpt, ResearchDocument
from app.rights.contracts import RightsMetadata, RightsScope, RightsStatus
from app.skills.execution import ToolAuthorizationError
from app.workers.research_skill_runtime import AuditedResearchProvider


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def __init__(self, records, *, authorized=True):
        self.records = records
        self.authorized = authorized

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def scalar(self, statement):
        return uuid4() if self.authorized else None

    def begin(self):
        return FakeTransaction()

    def add(self, record):
        self.records.append(record)


class FakeResearchProvider:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

    async def research(self, topic):
        self.calls.append(topic)
        return self.documents


def make_document():
    content = "Solar cells convert sunlight into electrical energy."
    return ResearchDocument(
        canonical_url="https://example.org/solar-cells",
        title="Solar cell overview",
        publisher="Example Research",
        source_type="other",
        published_at=None,
        retrieved_at=datetime.now(UTC),
        content=content,
        evidence=(EvidenceExcerpt(excerpt=content, locator="Overview"),),
    )


@pytest.mark.anyio
async def test_audited_research_provider_returns_documents_and_persists_provenance():
    records = []
    original = make_document()
    document = ResearchDocument(
        canonical_url=original.canonical_url,
        title=original.title,
        publisher=original.publisher,
        source_type=original.source_type,
        published_at=original.published_at,
        retrieved_at=original.retrieved_at,
        content=original.content,
        evidence=original.evidence,
        rights_metadata=RightsMetadata(
            status=RightsStatus.REVIEW_REQUIRED,
            scope=RightsScope.SOURCE_TEXT,
            license_ids=("CC-BY-SA-4.0",),
            license_urls=("https://creativecommons.org/licenses/by-sa/4.0/",),
            source_revision_url="https://example.org/revisions/17",
        ),
    )
    provider = FakeResearchProvider((document,))

    def session_factory():
        return FakeSession(records)

    audited = AuditedResearchProvider(
        provider,
        organization_id=uuid4(),
        actor_id=uuid4(),
        research_run_id=uuid4(),
        attempt_count=1,
        session_factory=session_factory,
    )

    results = await audited.research("solar energy")

    assert len(results) == 1
    assert results[0].canonical_url == document.canonical_url
    assert results[0].evidence[0].excerpt == document.evidence[0].excerpt
    assert results[0].rights_metadata == document.rights_metadata
    assert provider.calls == ["solar energy"]
    assert len(records) == 1
    assert records[0].skill_id == "research.source_retrieval"
    assert records[0].tool_id == "research.retrieve_sources"
    assert records[0].outcome == "success"
    assert records[0].output_sha256 is not None


@pytest.mark.anyio
async def test_audited_research_provider_denies_inactive_member_before_provider_call():
    records = []
    provider = FakeResearchProvider((make_document(),))

    def session_factory():
        return FakeSession(records, authorized=False)

    audited = AuditedResearchProvider(
        provider,
        organization_id=uuid4(),
        actor_id=uuid4(),
        research_run_id=uuid4(),
        attempt_count=1,
        session_factory=session_factory,
    )

    with pytest.raises(ToolAuthorizationError):
        await audited.research("solar energy")

    assert provider.calls == []
    assert len(records) == 1
    assert records[0].outcome == "failure"
    assert records[0].failure_code == "authorization_denied"
