from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_verified_user, require_organization_roles
from app.core.config import get_settings
from app.database.session import get_db_session
from app.models.evidence_item import EvidenceItem
from app.models.organization import Organization
from app.models.organization_source_rights_profile import OrganizationSourceRightsProfile
from app.models.research_run import ResearchRun
from app.models.research_source import ResearchSource
from app.models.research_source_rights_review import ResearchSourceRightsReview
from app.models.production_run import ProductionRun
from app.models.user import User
from app.research.normalization import content_sha256
from app.rights.contracts import RightsMetadata, RightsStatus
from app.schemas.research import (
    EvidenceItemResponse,
    ResearchRunCreateRequest,
    ResearchRunResponse,
    ResearchCleanupResponse,
    ResearchSourceResponse,
    ResearchSourceRightsReviewRequest,
    ResearchSourceRightsReviewResponse,
    ResearchSourceRightsReviewResult,
)
from app.schemas.studio_settings import OrganizationStudioSettings
from app.services.rate_limit import RateLimit, enforce_rate_limits
from app.services.research import create_research_run

router = APIRouter()


@router.post(
    "/organizations/{organization_id}/research-runs",
    response_model=ResearchRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a research run",
)
async def create_research_run_endpoint(
    payload: ResearchRunCreateRequest,
    organization_id: UUID,
    request: Request,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor")
    ),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunResponse:
    """Create a research run within an organization the user can access."""

    # Keep the path and authorized organization context consistent.
    # The authorization dependency resolves the organization from this path.
    assert organization.id == organization_id

    studio_settings = OrganizationStudioSettings.model_validate(
        organization.studio_settings or {}
    )
    if not studio_settings.research_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Research is paused in this workspace's Studio settings.",
        )

    settings = get_settings()
    await enforce_rate_limits(
        request,
        RateLimit(
            "research-run-user",
            settings.research_run_user_limit,
            3600,
            str(current_user.id),
        ),
        RateLimit(
            "research-run-organization",
            settings.research_run_organization_limit,
            3600,
            str(organization.id),
        ),
    )

    try:
        research_run = await create_research_run(
            session=session,
            organization_id=organization.id,
            requested_by_user_id=current_user.id,
            topic=payload.topic,
        )
        await session.commit()
        await session.refresh(research_run)

    except Exception:
        await session.rollback()
        raise

    return ResearchRunResponse.model_validate(research_run)


@router.get(
    "/organizations/{organization_id}/research-runs",
    response_model=list[ResearchRunResponse],
    summary="List research runs",
)
async def list_research_runs_endpoint(
    organization_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[ResearchRunResponse]:
    """List research runs visible to an active organization member."""
    assert organization.id == organization_id

    result = await session.scalars(
        select(ResearchRun)
        .where(ResearchRun.organization_id == organization.id)
        .order_by(ResearchRun.created_at.desc(), ResearchRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [ResearchRunResponse.model_validate(run) for run in result.all()]


@router.post(
    "/organizations/{organization_id}/research-runs/cleanup",
    response_model=ResearchCleanupResponse,
    summary="Remove safe-to-delete failed research history",
)
async def cleanup_research_runs_endpoint(
    organization_id: UUID,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    session: AsyncSession = Depends(get_db_session),
) -> ResearchCleanupResponse:
    """Remove failed research runs that are not referenced by production."""
    assert organization.id == organization_id
    referenced = select(ProductionRun.id).where(
        ProductionRun.organization_id == organization.id,
        ProductionRun.research_run_id == ResearchRun.id,
    ).exists()
    runs = list((await session.scalars(
        select(ResearchRun).where(
            ResearchRun.organization_id == organization.id,
            ResearchRun.status == "failed",
            ~referenced,
        )
    )).all())
    for run in runs:
        # Rights reviews intentionally do not use a cascading FK because they
        # are immutable audit records. They are safe to remove only alongside
        # a failed, unreferenced research run.
        await session.execute(
            delete(ResearchSourceRightsReview).where(
                ResearchSourceRightsReview.organization_id == organization.id,
                ResearchSourceRightsReview.research_run_id == run.id,
            )
        )
        await session.delete(run)
    await session.commit()
    return ResearchCleanupResponse(
        deleted_count=len(runs),
        skipped_count=0,
        skipped_statuses={},
    )


@router.get(
    "/organizations/{organization_id}/research-runs/{research_run_id}",
    response_model=ResearchRunResponse,
    summary="Get a research run",
)
async def get_research_run_endpoint(
    organization_id: UUID,
    research_run_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunResponse:
    """Get a research run only within the authorized organization."""
    assert organization.id == organization_id

    research_run = await session.scalar(
        select(ResearchRun).where(
            ResearchRun.id == research_run_id,
            ResearchRun.organization_id == organization.id,
        )
    )
    if research_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Research run not found.",
        )

    return ResearchRunResponse.model_validate(research_run)


@router.get(
    "/organizations/{organization_id}/research-runs/{research_run_id}/sources",
    response_model=list[ResearchSourceResponse],
    summary="List research sources",
)
async def list_research_sources_endpoint(
    organization_id: UUID,
    research_run_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[ResearchSourceResponse]:
    """List source provenance for a run visible to this organization."""
    assert organization.id == organization_id

    run_exists = await session.scalar(
        select(ResearchRun.id).where(
            ResearchRun.id == research_run_id,
            ResearchRun.organization_id == organization.id,
        )
    )
    if run_exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Research run not found.",
        )

    result = await session.scalars(
        select(ResearchSource)
        .where(ResearchSource.research_run_id == research_run_id)
        .order_by(ResearchSource.created_at.asc(), ResearchSource.id.asc())
        .limit(limit)
        .offset(offset)
    )
    return [ResearchSourceResponse.model_validate(source) for source in result.all()]


@router.get(
    "/organizations/{organization_id}/research-runs/{research_run_id}/sources/{source_id}/evidence",
    response_model=list[EvidenceItemResponse],
    summary="List evidence excerpts for a research source",
)
async def list_research_evidence_endpoint(
    organization_id: UUID,
    research_run_id: UUID,
    source_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[EvidenceItemResponse]:
    """List excerpts only after resolving the source through the authorized run."""
    assert organization.id == organization_id

    source_exists = await session.scalar(
        select(ResearchSource.id)
        .join(ResearchRun, ResearchRun.id == ResearchSource.research_run_id)
        .where(
            ResearchSource.id == source_id,
            ResearchSource.research_run_id == research_run_id,
            ResearchRun.organization_id == organization.id,
        )
    )
    if source_exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Research source not found.",
        )

    result = await session.scalars(
        select(EvidenceItem)
        .where(EvidenceItem.research_source_id == source_id)
        .order_by(EvidenceItem.id.asc())
        .limit(limit)
        .offset(offset)
    )
    return [EvidenceItemResponse.model_validate(item) for item in result.all()]


@router.post(
    "/organizations/{organization_id}/research-runs/{research_run_id}"
    "/sources/{source_id}/rights-reviews",
    response_model=ResearchSourceRightsReviewResult,
    status_code=status.HTTP_201_CREATED,
    summary="Review research-source reuse rights",
)
async def review_research_source_rights_endpoint(
    payload: ResearchSourceRightsReviewRequest,
    organization_id: UUID,
    research_run_id: UUID,
    source_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin")
    ),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> ResearchSourceRightsReviewResult:
    """Record an audited rights decision for a source within the authorized tenant."""
    assert organization.id == organization_id

    source = await session.scalar(
        select(ResearchSource)
        .join(ResearchRun, ResearchRun.id == ResearchSource.research_run_id)
        .where(
            ResearchSource.id == source_id,
            ResearchSource.research_run_id == research_run_id,
            ResearchRun.organization_id == organization.id,
        )
        .with_for_update()
    )
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Research source not found.",
        )

    current_source_rights = RightsMetadata.model_validate(
        source.rights_metadata or {}
    )
    if payload.source_revision_url != current_source_rights.source_revision_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rights review must reference the exact current source revision.",
        )

    canonical_url_hash = content_sha256(source.canonical_url)
    profile = await session.scalar(
        select(OrganizationSourceRightsProfile)
        .where(
            OrganizationSourceRightsProfile.organization_id == organization.id,
            OrganizationSourceRightsProfile.canonical_url_hash == canonical_url_hash,
        )
        .with_for_update()
    )
    if profile is None:
        profile_id = await session.scalar(
            pg_insert(OrganizationSourceRightsProfile)
            .values(
                organization_id=organization.id,
                canonical_url_hash=canonical_url_hash,
                canonical_url=source.canonical_url,
                rights_metadata=source.rights_metadata or {},
            )
            .on_conflict_do_nothing(
                constraint="uq_org_source_rights_profile_org_url_hash"
            )
            .returning(OrganizationSourceRightsProfile.id)
        )
        if profile_id is None:
            profile = await session.scalar(
                select(OrganizationSourceRightsProfile)
                .where(
                    OrganizationSourceRightsProfile.organization_id == organization.id,
                    OrganizationSourceRightsProfile.canonical_url_hash == canonical_url_hash,
                )
                .with_for_update()
            )
            if profile is None:
                raise RuntimeError("Unable to resolve source rights profile.")
            previous_rights = RightsMetadata.model_validate(profile.rights_metadata)
        else:
            profile = await session.scalar(
                select(OrganizationSourceRightsProfile).where(
                    OrganizationSourceRightsProfile.id == profile_id
                )
            )
            if profile is None:
                raise RuntimeError("Unable to resolve created source rights profile.")
            previous_rights = RightsMetadata.model_validate(source.rights_metadata or {})
    else:
        previous_rights = RightsMetadata.model_validate(profile.rights_metadata)

    if profile.canonical_url != source.canonical_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Source rights profile identity does not match the source URL.",
        )

    resulting_rights = RightsMetadata(
        status=(
            RightsStatus.CLEARED
            if payload.decision == "cleared"
            else RightsStatus.RESTRICTED
        ),
        scope=payload.scope,
        license_ids=payload.license_ids,
        license_urls=payload.license_urls,
        rights_holder=payload.rights_holder,
        creators=payload.creators,
        source_revision_url=payload.source_revision_url,
        attribution_text=payload.attribution_text,
        reuse_restrictions=payload.reuse_restrictions,
    )

    profile.canonical_url = source.canonical_url
    profile.rights_metadata = resulting_rights.model_dump(mode="json")
    source.rights_metadata = resulting_rights.model_dump(mode="json")

    review = ResearchSourceRightsReview(
        organization_id=organization.id,
        rights_profile_id=profile.id,
        research_run_id=research_run_id,
        research_source_id=source_id,
        reviewer_user_id=current_user.id,
        decision=payload.decision,
        review_notes=payload.review_notes,
        evidence_urls=list(payload.evidence_urls),
        previous_rights_metadata=previous_rights.model_dump(mode="json"),
        resulting_rights_metadata=resulting_rights.model_dump(mode="json"),
    )
    session.add(review)

    try:
        await session.commit()
        await session.refresh(source)
        await session.refresh(review)
    except Exception:
        await session.rollback()
        raise

    return ResearchSourceRightsReviewResult(
        review=ResearchSourceRightsReviewResponse.model_validate(review),
        source=ResearchSourceResponse.model_validate(source),
        requires_new_story_run=True,
    )


@router.get(
    "/organizations/{organization_id}/research-runs/{research_run_id}"
    "/sources/{source_id}/rights-reviews",
    response_model=list[ResearchSourceRightsReviewResponse],
    summary="List research-source rights review history",
)
async def list_research_source_rights_reviews_endpoint(
    organization_id: UUID,
    research_run_id: UUID,
    source_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[ResearchSourceRightsReviewResponse]:
    """List immutable rights decisions only within the authorized tenant."""
    assert organization.id == organization_id

    result = await session.scalars(
        select(ResearchSourceRightsReview)
        .where(
            ResearchSourceRightsReview.organization_id == organization.id,
            ResearchSourceRightsReview.research_run_id == research_run_id,
            ResearchSourceRightsReview.research_source_id == source_id,
        )
        .order_by(
            ResearchSourceRightsReview.created_at.desc(),
            ResearchSourceRightsReview.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return [
        ResearchSourceRightsReviewResponse.model_validate(review)
        for review in result.all()
    ]


@router.get(
    "/organizations/{organization_id}/source-rights-profiles/{rights_profile_id}/reviews",
    response_model=list[ResearchSourceRightsReviewResponse],
    summary="List tenant source-rights profile review history",
)
async def list_source_rights_profile_reviews_endpoint(
    organization_id: UUID,
    rights_profile_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[ResearchSourceRightsReviewResponse]:
    """List all decisions for a reusable tenant source-rights profile."""
    assert organization.id == organization_id

    profile_exists = await session.scalar(
        select(OrganizationSourceRightsProfile.id).where(
            OrganizationSourceRightsProfile.id == rights_profile_id,
            OrganizationSourceRightsProfile.organization_id == organization.id,
        )
    )
    if profile_exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source rights profile not found.",
        )

    result = await session.scalars(
        select(ResearchSourceRightsReview)
        .where(
            ResearchSourceRightsReview.organization_id == organization.id,
            ResearchSourceRightsReview.rights_profile_id == rights_profile_id,
        )
        .order_by(
            ResearchSourceRightsReview.created_at.desc(),
            ResearchSourceRightsReview.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return [
        ResearchSourceRightsReviewResponse.model_validate(review)
        for review in result.all()
    ]
