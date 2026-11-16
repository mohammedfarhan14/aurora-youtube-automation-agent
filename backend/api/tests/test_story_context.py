import asyncio
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from app.database.engine import get_session_factory
from app.models.evidence_item import EvidenceItem
from app.models.organization import Organization
from app.models.research_run import ResearchRun
from app.models.research_source import ResearchSource
from app.models.user import User
from app.models.youtube import YouTubeChannel
from app.models.youtube_analytics_review import YouTubeAnalyticsReview
from app.story.context import (
    StoryContextNotFoundError,
    load_story_research_context,
)


def seed_context(status: str = "completed"):
    user_id = uuid4()
    organization_id = uuid4()
    run_id = uuid4()
    source_id = uuid4()
    first_evidence_id, second_evidence_id = sorted((uuid4(), uuid4()))

    async def seed():
        async with get_session_factory()() as session:
            async with session.begin():
                session.add(
                    User(
                        id=user_id,
                        email=f"story-context-{user_id.hex}@example.com",
                        display_name="Story Context Test",
                    )
                )
                await session.flush()
                session.add(
                    Organization(
                        id=organization_id,
                        name="Story Context Test",
                        slug=f"story-context-{user_id.hex}",
                        created_by_user_id=user_id,
                    )
                )
                await session.flush()
                session.add(
                    ResearchRun(
                        id=run_id,
                        organization_id=organization_id,
                        requested_by_user_id=user_id,
                        topic="Battery storage",
                        status=status,
                    )
                )
                await session.flush()
                session.add(
                    ResearchSource(
                        id=source_id,
                        research_run_id=run_id,
                        canonical_url="https://example.com/battery-storage",
                        title="Battery Storage Overview",
                        publisher="Example Research",
                        source_type="webpage",
                        retrieved_at=datetime.now(UTC),
                        content_hash="a" * 64,
                        publisher_domain="example.com",
                        source_class="unknown",
                        independence_group_key="group-example",
                        independence_basis="publisher_domain",
                        classification_version="1",
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        EvidenceItem(
                            id=first_evidence_id,
                            research_source_id=source_id,
                            excerpt="A" * 700,
                            content_hash="b" * 64,
                            locator="section-1",
                        ),
                        EvidenceItem(
                            id=second_evidence_id,
                            research_source_id=source_id,
                            excerpt="B" * 700,
                            content_hash="c" * 64,
                            locator="section-2",
                        ),
                    ]
                )

    asyncio.run(seed())
    return user_id, organization_id, run_id, source_id, first_evidence_id, second_evidence_id


def cleanup_context(user_id: UUID, organization_id: UUID):
    async def cleanup():
        async with get_session_factory()() as session:
            async with session.begin():
                await session.delete(await session.get(Organization, organization_id))
                await session.delete(await session.get(User, user_id))

    asyncio.run(cleanup())


def test_load_story_context_is_tenant_scoped_and_requires_completed_run():
    user_id, organization_id, run_id, *_ = seed_context(status="queued")

    async def load():
        async with get_session_factory()() as session:
            return await load_story_research_context(
                session,
                organization_id=organization_id,
                research_run_id=run_id,
            )

    try:
        with pytest.raises(StoryContextNotFoundError):
            asyncio.run(load())
    finally:
        cleanup_context(user_id, organization_id)


def test_load_story_context_preserves_provenance_and_enforces_character_budget():
    (
        user_id,
        organization_id,
        run_id,
        source_id,
        first_evidence_id,
        second_evidence_id,
    ) = seed_context()

    async def load():
        async with get_session_factory()() as session:
            return await load_story_research_context(
                session,
                organization_id=organization_id,
                research_run_id=run_id,
                max_context_characters=1_000,
            )

    try:
        context = asyncio.run(load())

        assert context.topic == "Battery storage"
        assert context.research_run_id == run_id
        assert context.organization_id == organization_id
        assert len(context.sources) == 1
        assert context.sources[0].id == source_id
        assert context.sources[0].canonical_url == "https://example.com/battery-storage"
        assert context.sources[0].publisher_domain == "example.com"
        assert context.sources[0].source_class == "unknown"
        assert context.sources[0].independence_group_key == "group-example"
        assert context.sources[0].independence_basis == "publisher_domain"
        assert context.sources[0].classification_version == "1"
        assert context.sources[0].rights_metadata.status.value == "unknown"
        assert context.included_evidence_count == 1
        assert context.omitted_evidence_count == 1
        assert context.context_characters <= 1_000
        assert context.sources[0].evidence[0].id == first_evidence_id
        assert context.sources[0].evidence[0].id != second_evidence_id
        assert context.sources[0].evidence[0].excerpt == "A" * 700
    finally:
        cleanup_context(user_id, organization_id)


def test_load_story_context_counts_evidence_from_sources_outside_source_limit():
    user_id, organization_id, run_id, *_ = seed_context()
    extra_source_id = uuid4()

    async def add_extra_source():
        async with get_session_factory()() as session:
            async with session.begin():
                session.add(
                    ResearchSource(
                        id=extra_source_id,
                        research_run_id=run_id,
                        canonical_url="https://example.com/extra",
                        title="Additional source",
                        publisher="Example Research",
                        source_type="webpage",
                        retrieved_at=datetime.now(UTC),
                        content_hash="d" * 64,
                    )
                )
                await session.flush()
                session.add(
                    EvidenceItem(
                        id=uuid4(),
                        research_source_id=extra_source_id,
                        excerpt="Additional evidence",
                        content_hash="e" * 64,
                        locator="section-1",
                    )
                )

    asyncio.run(add_extra_source())

    async def load():
        async with get_session_factory()() as session:
            return await load_story_research_context(
                session,
                organization_id=organization_id,
                research_run_id=run_id,
                max_sources=1,
            )

    try:
        context = asyncio.run(load())
        assert context.omitted_source_count == 1
        assert context.included_evidence_count + context.omitted_evidence_count == 3
        assert context.omitted_evidence_count >= 1
    finally:
        cleanup_context(user_id, organization_id)


def test_load_story_context_rejects_another_tenants_organization_id():
    user_id, organization_id, run_id, *_ = seed_context()

    async def load():
        async with get_session_factory()() as session:
            return await load_story_research_context(
                session,
                organization_id=uuid4(),
                research_run_id=run_id,
            )

    try:
        with pytest.raises(StoryContextNotFoundError):
            asyncio.run(load())
    finally:
        cleanup_context(user_id, organization_id)


def test_story_context_includes_only_approved_learnings_for_selected_channel():
    user_id, organization_id, run_id, *_ = seed_context()
    selected_channel_id = uuid4()

    async def seed_reviews():
        async with get_session_factory()() as session:
            async with session.begin():
                selected_channel = YouTubeChannel(
                    id=selected_channel_id,
                    organization_id=organization_id,
                    channel_id=f"UC-{uuid4().hex[:20]}",
                    title="Selected context channel",
                    encrypted_refresh_token="test-token",
                    key_version=1,
                    granted_scopes="https://www.googleapis.com/auth/youtube.readonly",
                    connected_by_user_id=user_id,
                )
                other_channel = YouTubeChannel(
                    organization_id=organization_id,
                    channel_id=f"UC-{uuid4().hex[:20]}",
                    title="Other context channel",
                    encrypted_refresh_token="test-token",
                    key_version=1,
                    granted_scopes="https://www.googleapis.com/auth/youtube.readonly",
                    connected_by_user_id=user_id,
                )
                session.add_all([selected_channel, other_channel])
                await session.flush()
                now = datetime.now(UTC)
                session.add_all(
                    [
                        YouTubeAnalyticsReview(
                            organization_id=organization_id,
                            youtube_channel_id=selected_channel.id,
                            current_start_date=date(2026, 11, 1),
                            current_end_date=date(2026, 11, 7),
                            previous_start_date=date(2026, 10, 25),
                            previous_end_date=date(2026, 10, 31),
                            status="approved",
                            suggestion="Keep the intro concise.",
                            evidence_metrics={"views_change_percent": 12.0},
                            reviewed_by_user_id=user_id,
                            reviewed_at=now,
                        ),
                        YouTubeAnalyticsReview(
                            organization_id=organization_id,
                            youtube_channel_id=selected_channel.id,
                            current_start_date=date(2026, 11, 8),
                            current_end_date=date(2026, 11, 14),
                            previous_start_date=date(2026, 11, 1),
                            previous_end_date=date(2026, 11, 7),
                            status="dismissed",
                            suggestion="Dismissed cue must not be reused.",
                            evidence_metrics={"views_change_percent": -1.0},
                            reviewed_by_user_id=user_id,
                            reviewed_at=now,
                        ),
                        YouTubeAnalyticsReview(
                            organization_id=organization_id,
                            youtube_channel_id=other_channel.id,
                            current_start_date=date(2026, 11, 1),
                            current_end_date=date(2026, 11, 7),
                            previous_start_date=date(2026, 10, 25),
                            previous_end_date=date(2026, 10, 31),
                            status="approved",
                            suggestion="Other channel cue must not leak.",
                            evidence_metrics={"views_change_percent": 50.0},
                            reviewed_by_user_id=user_id,
                            reviewed_at=now,
                        ),
                    ]
                )

    asyncio.run(seed_reviews())

    async def load():
        async with get_session_factory()() as session:
            return await load_story_research_context(
                session,
                organization_id=organization_id,
                research_run_id=run_id,
                youtube_channel_id=selected_channel_id,
            )

    try:
        context = asyncio.run(load())
        assert context.approved_analytics_learnings == ("Keep the intro concise.",)
    finally:
        cleanup_context(user_id, organization_id)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_sources": 0}, "max_sources"),
        ({"max_evidence_per_source": 0}, "max_evidence_per_source"),
        ({"max_context_characters": 999}, "max_context_characters"),
    ],
)
def test_load_story_context_rejects_invalid_limits(overrides, message):
    user_id, organization_id, run_id, *_ = seed_context()

    async def load():
        async with get_session_factory()() as session:
            return await load_story_research_context(
                session,
                organization_id=organization_id,
                research_run_id=run_id,
                **overrides,
            )

    try:
        with pytest.raises(ValueError, match=message):
            asyncio.run(load())
    finally:
        cleanup_context(user_id, organization_id)
