import re
import unicodedata
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization import (
    OrganizationCreateRequest,
    OrganizationUpdateRequest,
)


class OrganizationSlugGenerationError(Exception):
    """Raised when a unique organization slug cannot be generated."""


def generate_slug_base(name: str) -> str:
    """Convert an organization name into a URL-friendly slug."""

    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name.lower())
    slug = slug.strip("-")

    return slug or f"organization-{uuid4().hex[:8]}"


async def create_organization(
    session: AsyncSession,
    current_user: User,
    payload: OrganizationCreateRequest,
) -> Organization:
    """Create an organization and its initial owner atomically."""

    if not session.in_transaction():
        raise RuntimeError("An active database transaction is required.")

    if session.new or session.dirty or session.deleted:
        raise RuntimeError("The database session contains unrelated pending changes.")

    name = payload.name.strip()
    base_slug = generate_slug_base(name)[:100].rstrip("-")

    for suffix in range(1, 101):
        suffix_text = "" if suffix == 1 else f"-{suffix}"
        slug = f"{base_slug[: 100 - len(suffix_text)].rstrip('-')}{suffix_text}"

        existing_slug = await session.scalar(
            select(Organization.id).where(Organization.slug == slug)
        )

        if existing_slug is not None:
            continue

        try:
            async with session.begin_nested():
                organization = Organization(
                    name=name,
                    slug=slug,
                    created_by_user_id=current_user.id,
                    plan="free",
                    status="active",
                )

                session.add(organization)
                await session.flush()

                membership = OrganizationMember(
                    organization_id=organization.id,
                    user_id=current_user.id,
                    role="owner",
                    status="active",
                    joined_at=datetime.now(UTC),
                )

                session.add(membership)
                await session.flush()

            return organization

        except IntegrityError as exc:
            original_error = exc.orig
            diagnostic = getattr(original_error, "diag", None)
            constraint_name = getattr(diagnostic, "constraint_name", None)

            if constraint_name == "uq_organizations_slug":
                continue

            raise

    raise OrganizationSlugGenerationError("Unable to generate a unique organization slug.")


async def list_user_organizations(
    session: AsyncSession,
    user_id: UUID,
) -> list[Organization]:
    """Return active organizations the user belongs to."""

    result = await session.scalars(
        select(Organization)
        .join(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == "active",
            Organization.status == "active",
        )
        .order_by(Organization.created_at.desc(), Organization.id)
    )

    return list(result.all())


async def get_user_organization(
    session: AsyncSession,
    organization_id: UUID,
    user_id: UUID,
) -> Organization | None:
    """Return an organization only if the user has active membership."""

    return await session.scalar(
        select(Organization)
        .join(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .where(
            Organization.id == organization_id,
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == "active",
            Organization.status == "active",
        )
    )


async def update_organization(
    session: AsyncSession,
    organization: Organization,
    payload: OrganizationUpdateRequest,
) -> Organization:
    """Update editable organization details."""

    organization.name = payload.name

    await session.flush()

    return organization
