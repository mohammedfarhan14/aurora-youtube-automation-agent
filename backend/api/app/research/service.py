import logging
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.evidence_item import EvidenceItem
from app.models.organization import Organization
from app.models.organization_source_rights_profile import OrganizationSourceRightsProfile
from app.models.research_run import ResearchRun
from app.models.research_source import ResearchSource
from app.models.youtube import YouTubeChannel
from app.models.youtube_channel_schedule import YouTubeChannelSchedule
from app.research.contracts import ResearchDocument, ResearchProvider
from app.research.normalization import (
    content_sha256,
    normalize_source_url,
)
from app.research.provenance import assess_source_provenance
from app.rights.contracts import RightsMetadata, RightsStatus
from app.schemas.studio_settings import OrganizationStudioSettings
from app.services.production import create_production_run
from app.services.youtube_oauth import channel_can_publish

logger = logging.getLogger(__name__)


class ResearchRunNotRunnableError(Exception):
    """Raised when a research run cannot be claimed for execution."""


class ResearchLeaseLostError(RuntimeError):
    """Raised when a research worker no longer owns an active lease."""


@dataclass(frozen=True, slots=True)
class ResearchExecutionResult:
    research_run_id: UUID
    status: str
    created_source_count: int = 0
    created_evidence_count: int = 0


def _validate_document(
    document: ResearchDocument,
) -> tuple[str, str, str | None, str, str]:
    """Validate and normalize a provider document before persistence."""

    canonical_url = normalize_source_url(document.canonical_url)
    title = document.title.strip()
    publisher = document.publisher.strip() if document.publisher else None
    source_type = document.source_type
    source_hash = content_sha256(document.content)

    if not title or len(title) > 500:
        raise ValueError("Research source title is invalid.")

    if publisher is not None and len(publisher) > 250:
        raise ValueError("Research source publisher is invalid.")

    if len(canonical_url) > 2048:
        raise ValueError("Research source URL is too long.")

    if source_type not in {"webpage", "paper", "video", "news", "other"}:
        raise ValueError("Research source type is invalid.")

    if document.retrieved_at.tzinfo is None or document.retrieved_at.utcoffset() is None:
        raise ValueError("Research source retrieval timestamp must be timezone-aware.")

    if document.published_at is not None and (
        document.published_at.tzinfo is None or document.published_at.utcoffset() is None
    ):
        raise ValueError("Research source publication timestamp must be timezone-aware.")

    if not document.evidence:
        raise ValueError("Research source contains no evidence excerpts.")
    if not isinstance(document.rights_metadata, RightsMetadata):
        raise ValueError("Research source rights metadata is invalid.")

    return canonical_url, title, publisher, source_type, source_hash


async def _mark_failed(
    session_factory: async_sessionmaker[AsyncSession],
    research_run_id: UUID,
    failure_code: str,
    failure_message: str,
) -> None:
    """Mark a currently executing direct research run as failed."""

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(ResearchRun)
                .where(
                    ResearchRun.id == research_run_id,
                    ResearchRun.status == "researching",
                )
                .values(
                    status="failed",
                    failure_code=failure_code,
                    failure_message=failure_message,
                    completed_at=func.now(),
                )
            )


async def _persist_documents(
    session_factory: async_sessionmaker[AsyncSession],
    research_run_id: UUID,
    documents: Sequence[ResearchDocument],
    *,
    lease_token: UUID | None = None,
) -> tuple[int, int]:
    """
    Persist research sources and evidence in one transaction.

    Duplicate canonical URLs are reused only when their content hashes match.
    Conflicting content for the same URL causes the entire transaction to roll
    back.

    When lease_token is provided, the worker must own an active lease.
    The research run row remains locked throughout persistence so another
    worker cannot recover or reclaim it during this transaction.
    """

    created_source_count = 0
    created_evidence_count = 0
    provenance = assess_source_provenance(documents)

    async with session_factory() as session:
        async with session.begin():
            # Worker executions must prove lease ownership before writing.
            if lease_token is not None:
                organization_id = await session.scalar(
                    select(ResearchRun.organization_id)
                    .where(
                        ResearchRun.id == research_run_id,
                        ResearchRun.status == "researching",
                        ResearchRun.lease_token == lease_token,
                        ResearchRun.lease_expires_at > func.clock_timestamp(),
                    )
                    .with_for_update()
                )
                if organization_id is None:
                    raise ResearchLeaseLostError("Research worker no longer owns an active lease.")
            else:
                organization_id = await session.scalar(
                    select(ResearchRun.organization_id).where(
                        ResearchRun.id == research_run_id
                    )
                )
                if organization_id is None:
                    raise ValueError("Research run not found for source persistence.")

            for document, source_provenance in zip(documents, provenance, strict=True):
                (
                    canonical_url,
                    title,
                    publisher,
                    source_type,
                    source_hash,
                ) = _validate_document(document)

                rights_metadata = document.rights_metadata
                rights_profile = await session.scalar(
                    select(OrganizationSourceRightsProfile)
                    .where(
                        OrganizationSourceRightsProfile.organization_id == organization_id,
                        OrganizationSourceRightsProfile.canonical_url_hash
                        == content_sha256(canonical_url),
                    )
                    .with_for_update()
                )
                if rights_profile is not None:
                    if rights_profile.canonical_url != canonical_url:
                        raise ValueError(
                            "Source rights profile canonical URL hash mismatch."
                        )
                    reviewed_rights = RightsMetadata.model_validate(
                        rights_profile.rights_metadata
                    )
                    if reviewed_rights.status == RightsStatus.RESTRICTED:
                        rights_metadata = reviewed_rights.model_copy(
                            update={
                                "source_revision_url": (
                                    document.rights_metadata.source_revision_url
                                )
                            }
                        )
                    elif (
                        reviewed_rights.status == RightsStatus.CLEARED
                        and reviewed_rights.source_revision_url
                        == document.rights_metadata.source_revision_url
                    ):
                        rights_metadata = reviewed_rights
                rights_metadata_payload = rights_metadata.model_dump(mode="json")

                source_statement = (
                    pg_insert(ResearchSource)
                    .values(
                        research_run_id=research_run_id,
                        canonical_url=canonical_url,
                        title=title,
                        publisher=publisher,
                        source_type=source_type,
                        published_at=document.published_at,
                        retrieved_at=document.retrieved_at,
                        content_hash=source_hash,
                        publisher_domain=source_provenance.publisher_domain,
                        source_class=source_provenance.source_class,
                        independence_group_key=source_provenance.independence_group_key,
                        independence_basis=source_provenance.independence_basis,
                        classification_version=source_provenance.classification_version,
                        rights_metadata=rights_metadata_payload,
                    )
                    .on_conflict_do_nothing(constraint="uq_research_sources_run_canonical_url")
                    .returning(ResearchSource.id)
                )

                source_result = await session.execute(source_statement)
                source_id = source_result.scalar_one_or_none()

                if source_id is None:
                    # The canonical URL already exists for this research run.
                    # Resolve the existing row and verify its content hash.
                    existing_source = (
                        await session.execute(
                            select(
                                ResearchSource.id,
                                ResearchSource.content_hash,
                                ResearchSource.rights_metadata,
                            ).where(
                                ResearchSource.research_run_id == research_run_id,
                                ResearchSource.canonical_url == canonical_url,
                            )
                        )
                    ).one_or_none()

                    if existing_source is None:
                        raise RuntimeError("Unable to resolve persisted research source.")

                    if existing_source.content_hash != source_hash:
                        raise ValueError(
                            "Conflicting content for the same canonical source URL."
                        )
                    if (
                        existing_source.rights_metadata
                        != rights_metadata_payload
                    ):
                        raise ValueError(
                            "Conflicting rights metadata for the same canonical source URL."
                        )

                    source_id = existing_source.id

                else:
                    created_source_count += 1

                if source_id is None:
                    raise RuntimeError("Unable to resolve persisted research source.")

                for evidence in document.evidence:
                    excerpt = evidence.excerpt.strip()

                    if not excerpt or len(excerpt) > 10000:
                        raise ValueError("Research evidence excerpt is invalid.")

                    locator = evidence.locator.strip() if evidence.locator else None

                    if locator is not None and len(locator) > 500:
                        raise ValueError("Research evidence locator is invalid.")

                    evidence_hash = content_sha256(excerpt)

                    evidence_statement = (
                        pg_insert(EvidenceItem)
                        .values(
                            research_source_id=source_id,
                            excerpt=excerpt,
                            content_hash=evidence_hash,
                            locator=locator,
                        )
                        .on_conflict_do_nothing(constraint="uq_evidence_items_source_content_hash")
                        .returning(EvidenceItem.id)
                    )

                    evidence_result = await session.execute(evidence_statement)

                    if evidence_result.scalar_one_or_none() is not None:
                        created_evidence_count += 1

            # Complete the run in the same transaction as source/evidence
            # persistence. If this transition fails, all inserts roll back.
            completion_statement = update(ResearchRun).where(
                ResearchRun.id == research_run_id,
                ResearchRun.status == "researching",
            )

            if lease_token is not None:
                completion_statement = completion_statement.where(
                    ResearchRun.lease_token == lease_token,
                    ResearchRun.lease_expires_at > func.clock_timestamp(),
                )

            completion_result = await session.execute(
                completion_statement.values(
                    status="completed",
                    failure_code=None,
                    failure_message=None,
                    completed_at=func.now(),
                    lease_token=None,
                    lease_expires_at=None,
                )
            )

            if completion_result.rowcount != 1:
                if lease_token is not None:
                    raise ResearchLeaseLostError(
                        "Research worker lost its lease before completion."
                    )

                raise RuntimeError("Research run state changed during execution.")

            completed_run = await session.scalar(
                select(ResearchRun)
                .where(ResearchRun.id == research_run_id)
            )
            if completed_run is not None and completed_run.youtube_channel_id is not None:
                channel = await session.scalar(
                    select(YouTubeChannel)
                    .where(
                        YouTubeChannel.id == completed_run.youtube_channel_id,
                        YouTubeChannel.organization_id == completed_run.organization_id,
                    )
                    .with_for_update()
                )
                schedule = await session.scalar(
                    select(YouTubeChannelSchedule)
                    .where(
                        YouTubeChannelSchedule.organization_id
                        == completed_run.organization_id,
                        YouTubeChannelSchedule.youtube_channel_id
                        == completed_run.youtube_channel_id,
                    )
                    .with_for_update()
                )
                if (
                    channel is not None
                    and channel.status == "active"
                    and channel_can_publish(channel.granted_scopes)
                    and schedule is not None
                    and schedule.enabled
                ):
                    if (
                        completed_run.scheduled_local_date is None
                        or completed_run.scheduled_for is None
                    ):
                        raise RuntimeError(
                            "A scheduled research run is missing its local date or timestamp."
                        )
                    organization = await session.get(
                        Organization, completed_run.organization_id
                    )
                    studio_settings = OrganizationStudioSettings.model_validate(
                        organization.studio_settings or {}
                        if organization is not None
                        else {}
                    )
                    if studio_settings.video_generation_enabled:
                        await create_production_run(
                            session,
                            organization_id=completed_run.organization_id,
                            requested_by_user_id=completed_run.requested_by_user_id,
                            research_run_id=completed_run.id,
                            youtube_channel_id=completed_run.youtube_channel_id,
                            idempotency_key=(
                                f"daily:{completed_run.youtube_channel_id}:"
                                f"{completed_run.scheduled_local_date.isoformat()}"
                            ),
                            scheduled_local_date=completed_run.scheduled_local_date,
                            scheduled_for=completed_run.scheduled_for,
                        )
                elif schedule is not None and (
                    channel is None
                    or channel.status != "active"
                    or not channel_can_publish(channel.granted_scopes)
                ):
                    schedule.enabled = False
                    schedule.next_run_at = None

    return created_source_count, created_evidence_count


async def execute_research_run(
    session_factory: async_sessionmaker[AsyncSession],
    provider: ResearchProvider,
    research_run_id: UUID,
) -> ResearchExecutionResult:
    """Execute a queued research run and persist its evidence atomically."""

    # Claim the run in a short transaction. Concurrent executions cannot
    # claim the same queued run.
    async with session_factory() as session:
        claim_result = await session.execute(
            update(ResearchRun)
            .where(
                ResearchRun.id == research_run_id,
                ResearchRun.status == "queued",
            )
            .values(
                status="researching",
                started_at=func.now(),
                completed_at=None,
                failure_code=None,
                failure_message=None,
            )
            .returning(ResearchRun.topic)
        )

        topic = claim_result.scalar_one_or_none()
        await session.commit()

    if topic is None:
        raise ResearchRunNotRunnableError("Research run does not exist or is not queued.")

    # Provider calls happen outside database transactions.
    try:
        documents = tuple(await provider.research(topic))

        if not documents:
            raise ValueError("Research provider returned no documents.")

    except Exception as exc:
        logger.error(
            "Research provider failed for run %s (error_type=%s)",
            research_run_id,
            type(exc).__name__,
        )

        await _mark_failed(
            session_factory,
            research_run_id,
            "provider_failed",
            "The research provider could not complete this run.",
        )

        return ResearchExecutionResult(
            research_run_id=research_run_id,
            status="failed",
        )

    # All source/evidence inserts and the completed transition share one
    # transaction. Any persistence failure rolls back the entire operation.
    try:
        source_count, evidence_count = await _persist_documents(
            session_factory,
            research_run_id,
            documents,
        )

    except Exception as exc:
        logger.error(
            "Research persistence failed for run %s (error_type=%s)",
            research_run_id,
            type(exc).__name__,
        )

        await _mark_failed(
            session_factory,
            research_run_id,
            "persistence_failed",
            "Research results could not be persisted.",
        )

        return ResearchExecutionResult(
            research_run_id=research_run_id,
            status="failed",
        )

    return ResearchExecutionResult(
        research_run_id=research_run_id,
        status="completed",
        created_source_count=source_count,
        created_evidence_count=evidence_count,
    )
