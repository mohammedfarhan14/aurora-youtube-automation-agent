from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_verified_user, require_organization_roles
from app.database.session import get_db_session
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization import (
    OrganizationCreateRequest,
    OrganizationResponse,
    OrganizationUpdateRequest,
)
from app.schemas.studio_settings import (
    OrganizationStudioSettings,
    OrganizationStudioSettingsResponse,
)
from app.services.organization import (
    OrganizationSlugGenerationError,
    create_organization,
    get_user_organization,
    list_user_organizations,
    update_organization,
)

router = APIRouter()


@router.get(
    "/{organization_id}/studio-settings",
    response_model=OrganizationStudioSettingsResponse,
    summary="Read tenant studio settings",
)
async def get_studio_settings_endpoint(
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationStudioSettingsResponse:
    """Return the tenant's workflow policy and creative defaults."""
    role = await session.scalar(
        select(OrganizationMember.role).where(
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.user_id == current_user.id,
            OrganizationMember.status == "active",
        )
    )
    return OrganizationStudioSettingsResponse(
        settings=OrganizationStudioSettings.model_validate(organization.studio_settings or {}),
        updated_at=organization.studio_settings_updated_at,
        updated_by_user_id=organization.studio_settings_updated_by_user_id,
        can_edit=role in {"owner", "admin"},
    )


@router.put(
    "/{organization_id}/studio-settings",
    response_model=OrganizationStudioSettingsResponse,
    summary="Update tenant studio settings",
)
async def update_studio_settings_endpoint(
    payload: OrganizationStudioSettings,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationStudioSettingsResponse:
    """Persist tenant workflow rules; secrets are intentionally not accepted here."""
    from datetime import UTC, datetime

    organization.studio_settings = payload.model_dump(mode="json")
    organization.studio_settings_updated_by_user_id = current_user.id
    organization.studio_settings_updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(organization)
    return OrganizationStudioSettingsResponse(
        settings=OrganizationStudioSettings.model_validate(organization.studio_settings),
        updated_at=organization.studio_settings_updated_at,
        updated_by_user_id=organization.studio_settings_updated_by_user_id,
        can_edit=True,
    )


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an organization",
)
async def create_organization_endpoint(
    payload: OrganizationCreateRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_verified_user),
) -> OrganizationResponse:
    """Create an organization and assign the authenticated user as owner."""

    try:
        organization = await create_organization(
            session=session,
            current_user=current_user,
            payload=payload,
        )
        await session.commit()

    except OrganizationSlugGenerationError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Unable to generate a unique organization slug.",
        ) from exc

    except Exception:
        await session.rollback()
        raise

    return OrganizationResponse.model_validate(organization)


@router.get(
    "",
    response_model=list[OrganizationResponse],
    status_code=status.HTTP_200_OK,
    summary="List the current user's organizations",
)
async def list_organizations(
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_verified_user),
) -> list[OrganizationResponse]:
    """List organizations where the authenticated user has active membership."""

    organizations = await list_user_organizations(
        session=session,
        user_id=current_user.id,
    )

    return [OrganizationResponse.model_validate(organization) for organization in organizations]


@router.get(
    "/{organization_id}",
    response_model=OrganizationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get an organization",
)
async def get_organization_endpoint(
    organization_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_verified_user),
) -> OrganizationResponse:
    """Retrieve an organization accessible to the authenticated user."""

    organization = await get_user_organization(
        session=session,
        organization_id=organization_id,
        user_id=current_user.id,
    )

    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found.",
        )

    return OrganizationResponse.model_validate(organization)


@router.patch(
    "/{organization_id}",
    response_model=OrganizationResponse,
    status_code=status.HTTP_200_OK,
    summary="Update an organization",
)
async def update_organization_endpoint(
    payload: OrganizationUpdateRequest,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationResponse:
    """Update an organization accessible to owners and admins."""

    try:
        updated_organization = await update_organization(
            session=session,
            organization=organization,
            payload=payload,
        )
        await session.commit()

        await session.refresh(updated_organization)

    except Exception:
        await session.rollback()
        raise

    return OrganizationResponse.model_validate(updated_organization)
