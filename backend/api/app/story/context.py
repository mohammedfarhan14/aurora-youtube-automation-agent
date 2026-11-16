"""Tenant-scoped, bounded retrieval context for story generation."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evidence_item import EvidenceItem
from app.models.research_run import ResearchRun
from app.models.research_source import ResearchSource
from app.models.youtube_analytics_review import YouTubeAnalyticsReview
from app.rights.contracts import RightsMetadata


class StoryContextNotFoundError(LookupError):
    """Raised when the run is missing, belongs to another tenant, or is not ready."""


@dataclass(frozen=True, slots=True)
class StoryEvidence:
    id: UUID
    excerpt: str
    locator: str | None


@dataclass(frozen=True, slots=True)
class StorySource:
    id: UUID
    canonical_url: str
    title: str
    publisher: str | None
    source_type: str
    published_at: datetime | None
    retrieved_at: datetime
    evidence: tuple[StoryEvidence, ...]
    publisher_domain: str | None = None
    source_class: str = "unknown"
    independence_group_key: str | None = None
    independence_basis: str = "unassessed"
    classification_version: str = "0"
    rights_metadata: RightsMetadata = field(default_factory=RightsMetadata)


@dataclass(frozen=True, slots=True)
class StoryResearchContext:
    research_run_id: UUID
    organization_id: UUID
    topic: str
    sources: tuple[StorySource, ...]
    included_evidence_count: int
    omitted_evidence_count: int
    omitted_source_count: int
    context_characters: int
    editorial_profile: dict | None = None
    approved_analytics_learnings: tuple[str, ...] = ()


async def load_story_research_context(
    session: AsyncSession,
    *,
    organization_id: UUID,
    research_run_id: UUID,
    editorial_profile: dict | None = None,
    youtube_channel_id: UUID | None = None,
    max_sources: int = 6,
    max_evidence_per_source: int = 3,
    max_context_characters: int = 12_000,
) -> StoryResearchContext:
    """Load completed research for one tenant with database-enforced bounds.

    The tenant and completed-run predicates are checked before child records are
    queried. Evidence is ranked per source in SQL and only the configured number
    of rows per selected source is materialized. Excerpts are never truncated.
    """

    if not 1 <= max_sources <= 100:
        raise ValueError("max_sources must be between 1 and 100.")
    if not 1 <= max_evidence_per_source <= 50:
        raise ValueError("max_evidence_per_source must be between 1 and 50.")
    if not 1_000 <= max_context_characters <= 200_000:
        raise ValueError("max_context_characters must be between 1000 and 200000.")

    run = await session.scalar(
        select(ResearchRun).where(
            ResearchRun.id == research_run_id,
            ResearchRun.organization_id == organization_id,
            ResearchRun.status == "completed",
        )
    )
    if run is None:
        raise StoryContextNotFoundError("Completed research run not found.")

    if editorial_profile is not None and not isinstance(editorial_profile, dict):
        raise ValueError("editorial_profile must be a JSON object.")
    profile_characters = len(
        json.dumps(editorial_profile or {}, ensure_ascii=False, separators=(",", ":"))
    )
    analytics_learnings: tuple[str, ...] = ()
    if youtube_channel_id is not None:
        approved_reviews = (
            await session.scalars(
                select(YouTubeAnalyticsReview.suggestion)
                .where(
                    YouTubeAnalyticsReview.organization_id == organization_id,
                    YouTubeAnalyticsReview.youtube_channel_id == youtube_channel_id,
                    YouTubeAnalyticsReview.status == "approved",
                    YouTubeAnalyticsReview.current_end_date
                    >= datetime.now(UTC).date() - timedelta(days=90),
                )
                .order_by(
                    YouTubeAnalyticsReview.reviewed_at.desc(),
                    YouTubeAnalyticsReview.created_at.desc(),
                )
                .limit(5)
            )
        ).all()
        analytics_learnings = tuple(
            value[:500] for value in approved_reviews if isinstance(value, str) and value.strip()
        )
    learning_characters = sum(len(value) for value in analytics_learnings)
    if len(run.topic) + profile_characters + learning_characters > max_context_characters:
        raise ValueError("Editorial context exceeds the story context character budget.")

    total_source_count = await session.scalar(
        select(func.count(ResearchSource.id)).where(ResearchSource.research_run_id == run.id)
    )
    source_rows = (
        await session.scalars(
            select(ResearchSource)
            .where(ResearchSource.research_run_id == run.id)
            .order_by(ResearchSource.created_at.asc(), ResearchSource.id.asc())
            .limit(max_sources)
        )
    ).all()

    total_evidence_count = await session.scalar(
        select(func.count(EvidenceItem.id))
        .join(
            ResearchSource,
            EvidenceItem.research_source_id == ResearchSource.id,
        )
        .where(ResearchSource.research_run_id == run.id)
    )

    if not source_rows:
        return StoryResearchContext(
            research_run_id=run.id,
            organization_id=organization_id,
            topic=run.topic,
            sources=(),
            included_evidence_count=0,
            omitted_evidence_count=total_evidence_count or 0,
            omitted_source_count=total_source_count or 0,
            context_characters=len(run.topic) + profile_characters + learning_characters,
            editorial_profile=editorial_profile,
            approved_analytics_learnings=analytics_learnings,
        )

    source_ids = [source.id for source in source_rows]
    ranked_evidence = (
        select(
            EvidenceItem.id.label("evidence_id"),
            func.row_number()
            .over(
                partition_by=EvidenceItem.research_source_id,
                order_by=EvidenceItem.id.asc(),
            )
            .label("evidence_rank"),
        )
        .where(EvidenceItem.research_source_id.in_(source_ids))
        .subquery()
    )
    evidence_rows = (
        await session.scalars(
            select(EvidenceItem)
            .join(
                ranked_evidence,
                EvidenceItem.id == ranked_evidence.c.evidence_id,
            )
            .where(ranked_evidence.c.evidence_rank <= max_evidence_per_source)
            .order_by(
                EvidenceItem.research_source_id.asc(),
                EvidenceItem.id.asc(),
            )
        )
    ).all()

    evidence_by_source: dict[UUID, list[EvidenceItem]] = {source_id: [] for source_id in source_ids}
    for item in evidence_rows:
        evidence_by_source[item.research_source_id].append(item)

    characters_used = len(run.topic) + profile_characters + learning_characters
    included_count = 0
    omitted_source_count = max(0, (total_source_count or 0) - len(source_rows))
    sources: list[StorySource] = []

    for source in source_rows:
        candidates = evidence_by_source[source.id]
        metadata_cost = (
            len(source.canonical_url)
            + len(source.title)
            + len(source.publisher or "")
            + len(source.source_type)
            + len(
                json.dumps(
                    source.rights_metadata or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        )
        if characters_used + metadata_cost > max_context_characters:
            omitted_source_count += 1
            continue

        characters_used += metadata_cost
        selected_evidence: list[StoryEvidence] = []

        for item in candidates:
            excerpt_cost = len(item.excerpt) + len(item.locator or "")
            if characters_used + excerpt_cost > max_context_characters:
                continue

            selected_evidence.append(
                StoryEvidence(
                    id=item.id,
                    excerpt=item.excerpt,
                    locator=item.locator,
                )
            )
            characters_used += excerpt_cost
            included_count += 1

        sources.append(
            StorySource(
                id=source.id,
                canonical_url=source.canonical_url,
                title=source.title,
                publisher=source.publisher,
                source_type=source.source_type,
                published_at=source.published_at,
                retrieved_at=source.retrieved_at,
                evidence=tuple(selected_evidence),
                publisher_domain=source.publisher_domain,
                source_class=source.source_class,
                independence_group_key=source.independence_group_key,
                independence_basis=source.independence_basis,
                classification_version=source.classification_version,
                rights_metadata=RightsMetadata.model_validate(source.rights_metadata or {}),
            )
        )

    return StoryResearchContext(
        research_run_id=run.id,
        organization_id=organization_id,
        topic=run.topic,
        sources=tuple(sources),
        included_evidence_count=included_count,
        omitted_evidence_count=max(0, (total_evidence_count or 0) - included_count),
        omitted_source_count=omitted_source_count,
        context_characters=characters_used,
        editorial_profile=editorial_profile,
        approved_analytics_learnings=analytics_learnings,
    )
