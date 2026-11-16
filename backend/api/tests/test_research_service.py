import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.database.engine import get_session_factory
from app.main import app
from app.models.evidence_item import EvidenceItem
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.organization_source_rights_profile import OrganizationSourceRightsProfile
from app.models.research_run import ResearchRun
from app.models.research_source import ResearchSource
from app.models.user import User
from app.research.contracts import EvidenceExcerpt, ResearchDocument
from app.research.normalization import content_sha256
from app.research.service import (
    ResearchRunNotRunnableError,
    _validate_document,
    execute_research_run,
)
from app.rights.contracts import (
    ReuseRestriction,
    RightsMetadata,
    RightsScope,
    RightsStatus,
)

client = TestClient(app)


class StaticProvider:
    def __init__(self, documents):
        self.documents = documents
        self.calls = 0

    async def research(self, topic: str):
        self.calls += 1
        return self.documents


class FailingProvider:
    def __init__(self):
        self.calls = 0

    async def research(self, topic: str):
        self.calls += 1
        raise RuntimeError("Synthetic provider failure secret=legacy-provider-token-DO-NOT-LOG")


def create_test_context():
    email = f"research-service-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Research Service Test",
            "password": password,
        },
    )
    assert registration.status_code == 201
    user_data = registration.json()
    user_id = user_data["id"]

    async def verify_user():
        async with get_session_factory()() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.email_verified_at = datetime.now(UTC)
            await session.commit()

    asyncio.run(verify_user())

    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    organization_response = client.post(
        "/api/v1/organizations",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"Research Service {uuid4().hex[:8]}"},
    )
    assert organization_response.status_code == 201
    organization_id = organization_response.json()["id"]

    run_response = client.post(
        f"/api/v1/organizations/{organization_id}/research-runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"topic": "Renewable energy storage"},
    )
    assert run_response.status_code == 201

    return user_id, run_response.json()["id"]


def cleanup_test_context(user_id: str):
    async def cleanup():
        async with get_session_factory()() as session:
            async with session.begin():
                organization_ids = select(Organization.id).where(
                    Organization.created_by_user_id == user_id
                )
                await session.execute(
                    delete(ResearchRun).where(ResearchRun.organization_id.in_(organization_ids))
                )
                await session.execute(
                    delete(OrganizationMember).where(OrganizationMember.user_id == user_id)
                )
                await session.execute(
                    delete(Organization).where(Organization.created_by_user_id == user_id)
                )
                await session.execute(delete(User).where(User.id == user_id))

    asyncio.run(cleanup())


def make_documents():
    return (
        ResearchDocument(
            canonical_url="https://example.com/energy/overview",
            title="Energy Storage Overview",
            publisher="Example Research",
            source_type="webpage",
            published_at=None,
            retrieved_at=datetime(2026, 11, 10, tzinfo=UTC),
            content="Synthetic content about energy storage.",
            evidence=(
                EvidenceExcerpt(
                    excerpt="Synthetic evidence about battery storage.",
                    locator="section-1",
                ),
            ),
            rights_metadata=RightsMetadata(
                status=RightsStatus.CLEARED,
                scope=RightsScope.SOURCE_TEXT,
                license_ids=("CC0-1.0",),
                license_urls=("https://creativecommons.org/publicdomain/zero/1.0/",),
                rights_holder="Example Research",
                creators=("Example Research",),
                source_revision_url="https://example.com/energy/overview/revision/1",
                attribution_text="Example Research, Energy Storage Overview, CC0 1.0",
            ),
        ),
        ResearchDocument(
            canonical_url="https://example.org/energy/report",
            title="Energy Storage Report",
            publisher="Example Institute",
            source_type="paper",
            published_at=None,
            retrieved_at=datetime(2026, 11, 10, tzinfo=UTC),
            content="Synthetic report about energy storage.",
            evidence=(
                EvidenceExcerpt(
                    excerpt="Synthetic evidence about grid storage.",
                    locator="page-2",
                ),
            ),
        ),
    )


def test_validate_document_rejects_naive_retrieval_timestamp():
    document = make_documents()[0]
    document = ResearchDocument(
        canonical_url=document.canonical_url,
        title=document.title,
        publisher=document.publisher,
        source_type=document.source_type,
        published_at=document.published_at,
        retrieved_at=datetime(2026, 11, 10),
        content=document.content,
        evidence=document.evidence,
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        _validate_document(document)


def test_validate_document_rejects_naive_publication_timestamp():
    document = make_documents()[0]
    document = ResearchDocument(
        canonical_url=document.canonical_url,
        title=document.title,
        publisher=document.publisher,
        source_type=document.source_type,
        published_at=datetime(2026, 11, 10),
        retrieved_at=document.retrieved_at,
        content=document.content,
        evidence=document.evidence,
    )

    with pytest.raises(ValueError, match="publication timestamp must be timezone-aware"):
        _validate_document(document)


def test_execute_research_run_persists_sources_and_evidence():
    user_id, run_id = create_test_context()
    provider = StaticProvider(make_documents())

    try:
        result = asyncio.run(
            execute_research_run(
                get_session_factory(),
                provider,
                run_id,
            )
        )

        assert result.status == "completed"
        assert result.created_source_count == 2
        assert result.created_evidence_count == 2
        assert provider.calls == 1

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)
                assert run is not None
                assert run.status == "completed"
                assert run.started_at is not None
                assert run.completed_at is not None
                assert run.failure_code is None

                source_count = await session.scalar(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.research_run_id == run_id)
                )
                evidence_count = await session.scalar(
                    select(func.count())
                    .select_from(EvidenceItem)
                    .join(
                        ResearchSource,
                        EvidenceItem.research_source_id == ResearchSource.id,
                    )
                    .where(ResearchSource.research_run_id == run_id)
                )

                assert source_count == 2
                assert evidence_count == 2
                persisted_sources = list(
                    await session.scalars(
                        select(ResearchSource).where(
                            ResearchSource.research_run_id == run_id
                        )
                    )
                )
                by_domain = {
                    source.publisher_domain: source for source in persisted_sources
                }
                assert by_domain["example.com"].source_class == "unknown"
                assert by_domain["example.com"].independence_basis == "unique_source"
                assert by_domain["example.org"].source_class == "scholarly"
                assert all(
                    source.independence_group_key for source in persisted_sources
                )
                assert all(
                    source.classification_version == "1" for source in persisted_sources
                )
                example_com_rights = by_domain["example.com"].rights_metadata
                example_org_rights = by_domain["example.org"].rights_metadata
                assert example_com_rights["status"] == "cleared"
                assert example_com_rights["license_ids"] == ["CC0-1.0"]
                assert example_com_rights["rights_holder"] == "Example Research"
                assert example_org_rights["status"] == "unknown"
                retrieved_at_values = await session.scalars(
                    select(ResearchSource.retrieved_at).where(
                        ResearchSource.research_run_id == run_id
                    )
                )
                assert set(retrieved_at_values.all()) == {
                    datetime(2026, 11, 10, tzinfo=UTC)
                }

        asyncio.run(verify())
    finally:
        cleanup_test_context(user_id)


def test_execute_research_run_deduplicates_sources_and_evidence():
    user_id, run_id = create_test_context()

    duplicate_documents = (
        ResearchDocument(
            canonical_url="https://example.com/energy/overview",
            title="Original title",
            publisher="Original publisher",
            source_type="webpage",
            published_at=None,
            retrieved_at=datetime(2026, 11, 10, tzinfo=UTC),
            content="Synthetic content.",
            evidence=(EvidenceExcerpt(excerpt="Repeated evidence."),),
        ),
        ResearchDocument(
            canonical_url="HTTPS://EXAMPLE.COM/energy/overview#section",
            title="Duplicate title",
            publisher="Duplicate publisher",
            source_type="webpage",
            published_at=None,
            retrieved_at=datetime(2026, 11, 10, tzinfo=UTC),
            content="Synthetic content.",
            evidence=(EvidenceExcerpt(excerpt="Repeated evidence."),),
        ),
    )
    provider = StaticProvider(duplicate_documents)

    try:
        result = asyncio.run(
            execute_research_run(
                get_session_factory(),
                provider,
                run_id,
            )
        )

        assert result.status == "completed"
        assert result.created_source_count == 1
        assert result.created_evidence_count == 1

        async def verify():
            async with get_session_factory()() as session:
                source_count = await session.scalar(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.research_run_id == run_id)
                )
                evidence_count = await session.scalar(
                    select(func.count())
                    .select_from(EvidenceItem)
                    .join(
                        ResearchSource,
                        EvidenceItem.research_source_id == ResearchSource.id,
                    )
                    .where(ResearchSource.research_run_id == run_id)
                )
                assert source_count == 1
                assert evidence_count == 1

        asyncio.run(verify())
    finally:
        cleanup_test_context(user_id)


def test_execute_research_run_marks_provider_failure(caplog):
    user_id, run_id = create_test_context()
    provider = FailingProvider()

    try:
        result = asyncio.run(
            execute_research_run(
                get_session_factory(),
                provider,
                run_id,
            )
        )

        assert result.status == "failed"
        assert provider.calls == 1

        assert "legacy-provider-token-DO-NOT-LOG" not in caplog.text
        assert "RuntimeError" in caplog.text
        assert f"run {run_id}" in caplog.text

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)
                assert run is not None
                assert run.status == "failed"
                assert run.failure_code == "provider_failed"
                assert run.completed_at is not None

                source_count = await session.scalar(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.research_run_id == run_id)
                )
                assert source_count == 0

        asyncio.run(verify())
    finally:
        cleanup_test_context(user_id)


def test_execute_research_run_rolls_back_invalid_evidence():
    user_id, run_id = create_test_context()

    invalid_document = ResearchDocument(
        canonical_url="https://example.com/invalid",
        title="Synthetic Invalid Evidence",
        publisher="Example",
        source_type="webpage",
        published_at=None,
            retrieved_at=datetime(2026, 11, 10, tzinfo=UTC),
        content="Synthetic content.",
        evidence=(
            EvidenceExcerpt(excerpt="This valid excerpt is inserted first."),
            EvidenceExcerpt(excerpt="x" * 10001),
        ),
    )
    provider = StaticProvider((invalid_document,))

    try:
        result = asyncio.run(
            execute_research_run(
                get_session_factory(),
                provider,
                run_id,
            )
        )

        assert result.status == "failed"

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)
                assert run is not None
                assert run.status == "failed"
                assert run.failure_code == "persistence_failed"

                source_count = await session.scalar(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.research_run_id == run_id)
                )
                evidence_count = await session.scalar(
                    select(func.count())
                    .select_from(EvidenceItem)
                    .join(
                        ResearchSource,
                        EvidenceItem.research_source_id == ResearchSource.id,
                    )
                    .where(ResearchSource.research_run_id == run_id)
                )

                assert source_count == 0
                assert evidence_count == 0

        asyncio.run(verify())
    finally:
        cleanup_test_context(user_id)


def test_concurrent_execution_only_claims_run_once():
    user_id, run_id = create_test_context()
    provider = StaticProvider(make_documents())

    async def execute_twice():
        return await asyncio.gather(
            execute_research_run(
                get_session_factory(),
                provider,
                run_id,
            ),
            execute_research_run(
                get_session_factory(),
                provider,
                run_id,
            ),
            return_exceptions=True,
        )

    try:
        results = asyncio.run(execute_twice())

        successful = [item for item in results if not isinstance(item, BaseException)]
        rejected = [item for item in results if isinstance(item, ResearchRunNotRunnableError)]

        assert len(successful) == 1
        assert len(rejected) == 1
        assert successful[0].status == "completed"
        assert provider.calls == 1
    finally:
        cleanup_test_context(user_id)


def test_execute_research_run_rejects_conflicting_content_for_same_url():
    user_id, run_id = create_test_context()

    documents = (
        ResearchDocument(
            canonical_url="https://example.com/shared-source",
            title="Original source",
            publisher="Example",
            source_type="webpage",
            published_at=None,
            retrieved_at=datetime(2026, 11, 10, tzinfo=UTC),
            content="First version of the source.",
            evidence=(EvidenceExcerpt(excerpt="Evidence from first version."),),
        ),
        ResearchDocument(
            canonical_url="HTTPS://EXAMPLE.COM/shared-source#section",
            title="Updated source",
            publisher="Example",
            source_type="webpage",
            published_at=None,
            retrieved_at=datetime(2026, 11, 10, tzinfo=UTC),
            content="Different version of the same source.",
            evidence=(EvidenceExcerpt(excerpt="Evidence from second version."),),
        ),
    )
    provider = StaticProvider(documents)

    try:
        result = asyncio.run(
            execute_research_run(
                get_session_factory(),
                provider,
                run_id,
            )
        )

        assert result.status == "failed"
        assert provider.calls == 1

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)
                assert run is not None
                assert run.status == "failed"
                assert run.failure_code == "persistence_failed"

                source_count = await session.scalar(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.research_run_id == run_id)
                )
                evidence_count = await session.scalar(
                    select(func.count())
                    .select_from(EvidenceItem)
                    .join(
                        ResearchSource,
                        EvidenceItem.research_source_id == ResearchSource.id,
                    )
                    .where(ResearchSource.research_run_id == run_id)
                )

                assert source_count == 0
                assert evidence_count == 0

        asyncio.run(verify())
    finally:
        cleanup_test_context(user_id)


def test_execute_research_run_rejects_conflicting_rights_metadata_for_same_url():
    user_id, run_id = create_test_context()
    original = make_documents()[0]
    conflicting = ResearchDocument(
        canonical_url="HTTPS://EXAMPLE.COM/energy/overview#different-fragment",
        title=original.title,
        publisher=original.publisher,
        source_type=original.source_type,
        published_at=original.published_at,
        retrieved_at=original.retrieved_at,
        content=original.content,
        evidence=original.evidence,
        rights_metadata=RightsMetadata(),
    )
    provider = StaticProvider((original, conflicting))

    try:
        result = asyncio.run(
            execute_research_run(
                get_session_factory(),
                provider,
                run_id,
            )
        )

        assert result.status == "failed"

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)
                assert run is not None
                assert run.status == "failed"
                assert run.failure_code == "persistence_failed"
                source_count = await session.scalar(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.research_run_id == run_id)
                )
                assert source_count == 0

        asyncio.run(verify())
    finally:
        cleanup_test_context(user_id)


def test_research_run_inherits_reviewed_tenant_source_rights_profile():
    user_id, run_id = create_test_context()
    document = make_documents()[1]
    reviewed_rights = RightsMetadata(
        status=RightsStatus.CLEARED,
        scope=RightsScope.SOURCE_TEXT,
        license_ids=("CC0-1.0",),
        license_urls=("https://creativecommons.org/publicdomain/zero/1.0/",),
        rights_holder="Example Institute",
        creators=("Example Institute",),
        attribution_text="Example Institute, Energy Storage Report, CC0 1.0",
    )

    async def seed_profile():
        async with get_session_factory()() as session:
            run = await session.get(ResearchRun, run_id)
            assert run is not None
            session.add(
                OrganizationSourceRightsProfile(
                    organization_id=run.organization_id,
                    canonical_url_hash=content_sha256(document.canonical_url),
                    canonical_url=document.canonical_url,
                    rights_metadata=reviewed_rights.model_dump(mode="json"),
                )
            )
            await session.commit()

    try:
        asyncio.run(seed_profile())
        result = asyncio.run(
            execute_research_run(
                get_session_factory(),
                StaticProvider((document,)),
                run_id,
            )
        )
        assert result.status == "completed"

        async def verify_source_rights():
            async with get_session_factory()() as session:
                source = await session.scalar(
                    select(ResearchSource).where(
                        ResearchSource.research_run_id == run_id,
                        ResearchSource.canonical_url == document.canonical_url,
                    )
                )
                assert source is not None
                assert source.rights_metadata["status"] == "cleared"
                assert source.rights_metadata["license_ids"] == ["CC0-1.0"]
                assert source.rights_metadata["attribution_text"] == (
                    "Example Institute, Energy Storage Report, CC0 1.0"
                )

        asyncio.run(verify_source_rights())
    finally:
        cleanup_test_context(user_id)


def test_research_run_does_not_reuse_cleared_profile_for_different_revision():
    user_id, run_id = create_test_context()
    document = replace(
        make_documents()[1],
        rights_metadata=RightsMetadata(
            status=RightsStatus.REVIEW_REQUIRED,
            scope=RightsScope.SOURCE_TEXT,
            source_revision_url="https://example.org/energy/report/revision/2",
            reuse_restrictions=(ReuseRestriction.REVIEW_PAGE_HISTORY,),
        ),
    )
    reviewed_rights = RightsMetadata(
        status=RightsStatus.CLEARED,
        scope=RightsScope.SOURCE_TEXT,
        license_ids=("CC0-1.0",),
        license_urls=("https://creativecommons.org/publicdomain/zero/1.0/",),
        rights_holder="Example Institute",
        creators=("Example Institute",),
        source_revision_url="https://example.org/energy/report/revision/1",
        attribution_text="Example Institute, Energy Storage Report, CC0 1.0",
    )

    async def seed_profile():
        async with get_session_factory()() as session:
            run = await session.get(ResearchRun, run_id)
            assert run is not None
            session.add(
                OrganizationSourceRightsProfile(
                    organization_id=run.organization_id,
                    canonical_url_hash=content_sha256(document.canonical_url),
                    canonical_url=document.canonical_url,
                    rights_metadata=reviewed_rights.model_dump(mode="json"),
                )
            )
            await session.commit()

    try:
        asyncio.run(seed_profile())
        result = asyncio.run(
            execute_research_run(
                get_session_factory(),
                StaticProvider((document,)),
                run_id,
            )
        )
        assert result.status == "completed"

        async def verify_source_revision():
            async with get_session_factory()() as session:
                source = await session.scalar(
                    select(ResearchSource).where(
                        ResearchSource.research_run_id == run_id,
                        ResearchSource.canonical_url == document.canonical_url,
                    )
                )
                assert source is not None
                assert source.rights_metadata["status"] == "review_required"
                assert source.rights_metadata["source_revision_url"] == (
                    "https://example.org/energy/report/revision/2"
                )

        asyncio.run(verify_source_revision())
    finally:
        cleanup_test_context(user_id)


def test_restricted_profile_applies_across_revisions_but_tracks_current_revision():
    user_id, run_id = create_test_context()
    current_revision_url = "https://example.org/energy/report/revision/2"
    document = replace(
        make_documents()[1],
        rights_metadata=RightsMetadata(
            status=RightsStatus.REVIEW_REQUIRED,
            scope=RightsScope.SOURCE_TEXT,
            source_revision_url=current_revision_url,
            reuse_restrictions=(ReuseRestriction.REVIEW_PAGE_HISTORY,),
        ),
    )
    restricted_rights = RightsMetadata(
        status=RightsStatus.RESTRICTED,
        scope=RightsScope.SOURCE_TEXT,
        source_revision_url="https://example.org/energy/report/revision/1",
        reuse_restrictions=(ReuseRestriction.NO_DERIVATIVES,),
    )

    async def seed_profile():
        async with get_session_factory()() as session:
            run = await session.get(ResearchRun, run_id)
            assert run is not None
            session.add(
                OrganizationSourceRightsProfile(
                    organization_id=run.organization_id,
                    canonical_url_hash=content_sha256(document.canonical_url),
                    canonical_url=document.canonical_url,
                    rights_metadata=restricted_rights.model_dump(mode="json"),
                )
            )
            await session.commit()

    try:
        asyncio.run(seed_profile())
        result = asyncio.run(
            execute_research_run(
                get_session_factory(),
                StaticProvider((document,)),
                run_id,
            )
        )
        assert result.status == "completed"

        async def verify_current_revision():
            async with get_session_factory()() as session:
                source = await session.scalar(
                    select(ResearchSource).where(
                        ResearchSource.research_run_id == run_id,
                        ResearchSource.canonical_url == document.canonical_url,
                    )
                )
                assert source is not None
                assert source.rights_metadata["status"] == "restricted"
                assert source.rights_metadata["source_revision_url"] == (
                    current_revision_url
                )

        asyncio.run(verify_current_revision())
    finally:
        cleanup_test_context(user_id)
