from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import require_organization_roles
from app.database.engine import get_session_factory
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.schemas.organization import OrganizationCreateRequest
from app.services.auth import register_user
from app.services.organization import create_organization


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def create_test_user(email: str) -> User:
    factory = get_session_factory()

    async with factory() as session:
        return await register_user(
            session,
            RegisterRequest(
                email=email,
                display_name="Aurora Test User",
                password="Aurora-Secure-Password-2026",
            ),
        )


async def cleanup_test_data(user_id):
    factory = get_session_factory()

    async with factory() as session:
        async with session.begin():
            await session.execute(
                delete(OrganizationMember).where(OrganizationMember.user_id == user_id)
            )
            await session.execute(
                delete(Organization).where(Organization.created_by_user_id == user_id)
            )
            await session.execute(delete(User).where(User.id == user_id))


@pytest.mark.anyio
async def test_create_organization_creates_owner_membership():
    email = f"organization-{uuid4().hex}@example.com"
    user = await create_test_user(email)

    try:
        factory = get_session_factory()

        async with factory() as session:
            current_user = await session.get(User, user.id)

            organization = await create_organization(
                session,
                current_user,
                OrganizationCreateRequest(name=f"Aurora Studio {uuid4().hex}"),
            )

            await session.commit()

            assert organization.name.startswith("Aurora Studio ")
            assert organization.slug.startswith("aurora-studio-")
            assert organization.plan == "free"
            assert organization.status == "active"

        async with factory() as session:
            membership = await session.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == organization.id,
                    OrganizationMember.user_id == user.id,
                )
            )

            assert membership is not None
            assert membership.role == "owner"
            assert membership.status == "active"
            assert membership.joined_at is not None

    finally:
        await cleanup_test_data(user.id)


@pytest.mark.anyio
async def test_create_organization_generates_unique_slug():
    email = f"organization-slug-{uuid4().hex}@example.com"
    user = await create_test_user(email)

    try:
        factory = get_session_factory()

        async with factory() as session:
            current_user = await session.get(User, user.id)

            first = await create_organization(
                session,
                current_user,
                OrganizationCreateRequest(name=f"Aurora Studio {uuid4().hex}"),
            )

            await session.commit()

        async with factory() as session:
            current_user = await session.get(User, user.id)

            second = await create_organization(
                session,
                current_user,
                OrganizationCreateRequest(name=first.name),
            )

            await session.commit()

            assert second.slug == f"{first.slug}-2"
            assert first.id != second.id

    finally:
        await cleanup_test_data(user.id)


@pytest.mark.anyio
async def test_create_organization_limits_slug_to_100_characters():
    email = f"organization-long-{uuid4().hex}@example.com"
    user = await create_test_user(email)

    try:
        long_name = "A" * 160
        factory = get_session_factory()

        async with factory() as session:
            current_user = await session.get(User, user.id)

            organization = await create_organization(
                session,
                current_user,
                OrganizationCreateRequest(name=long_name),
            )

            await session.commit()

            assert len(organization.slug) <= 100
            assert organization.slug == "a" * 100

    finally:
        await cleanup_test_data(user.id)


@pytest.mark.anyio
async def test_create_organization_rolls_back_when_owner_membership_fails():
    email = f"organization-rollback-{uuid4().hex}@example.com"
    organization_name = f"Rollback Test {uuid4().hex}"

    user = await create_test_user(email)

    def fail_membership_insert(mapper, connection, target):
        raise IntegrityError(
            "Simulated membership insertion failure",
            {},
            RuntimeError("Simulated database failure"),
        )

    event.listen(
        OrganizationMember,
        "before_insert",
        fail_membership_insert,
    )

    try:
        factory = get_session_factory()

        async with factory() as session:
            current_user = await session.get(User, user.id)

            with pytest.raises(IntegrityError):
                await create_organization(
                    session,
                    current_user,
                    OrganizationCreateRequest(name=organization_name),
                )

        # Verify using a separate database session.
        async with factory() as verification_session:
            organization = await verification_session.scalar(
                select(Organization).where(
                    Organization.created_by_user_id == user.id,
                    Organization.name == organization_name,
                )
            )

            assert organization is None

    finally:
        event.remove(
            OrganizationMember,
            "before_insert",
            fail_membership_insert,
        )
        await cleanup_test_data(user.id)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("role", "expected_status"),
    [
        ("owner", None),
        ("admin", None),
        ("editor", 403),
        ("viewer", 403),
    ],
)
async def test_organization_role_authorization(role, expected_status):
    email = f"organization-auth-{uuid4().hex}@example.com"
    user = await create_test_user(email)

    try:
        factory = get_session_factory()

        async with factory() as session:
            current_user = await session.get(User, user.id)

            organization = await create_organization(
                session,
                current_user,
                OrganizationCreateRequest(name="Authorization Test"),
            )

            await session.commit()

            membership = await session.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == organization.id,
                    OrganizationMember.user_id == user.id,
                )
            )

            membership.role = role
            await session.commit()

        async with factory() as session:
            current_user = await session.get(User, user.id)

            authorize = require_organization_roles("owner", "admin")

            if expected_status is None:
                result = await authorize(
                    organization_id=organization.id,
                    session=session,
                    current_user=current_user,
                )
                assert result.id == organization.id
            else:
                with pytest.raises(HTTPException) as exc_info:
                    await authorize(
                        organization_id=organization.id,
                        session=session,
                        current_user=current_user,
                    )

                assert exc_info.value.status_code == expected_status

    finally:
        await cleanup_test_data(user.id)


@pytest.mark.anyio
async def test_organization_role_authorization_rejects_non_member():
    owner = await create_test_user(f"organization-owner-{uuid4().hex}@example.com")
    outsider = await create_test_user(f"organization-outsider-{uuid4().hex}@example.com")

    try:
        factory = get_session_factory()

        async with factory() as session:
            current_owner = await session.get(User, owner.id)

            organization = await create_organization(
                session,
                current_owner,
                OrganizationCreateRequest(name="Private Organization"),
            )

            await session.commit()

        async with factory() as session:
            current_outsider = await session.get(User, outsider.id)

            authorize = require_organization_roles("owner", "admin")

            with pytest.raises(HTTPException) as exc_info:
                await authorize(
                    organization_id=organization.id,
                    session=session,
                    current_user=current_outsider,
                )

            assert exc_info.value.status_code == 404
            assert exc_info.value.detail == "Organization not found."

    finally:
        await cleanup_test_data(owner.id)
        await cleanup_test_data(outsider.id)


@pytest.mark.anyio
async def test_organization_role_authorization_rejects_suspended_member():
    email = f"organization-suspended-{uuid4().hex}@example.com"
    user = await create_test_user(email)

    try:
        factory = get_session_factory()

        async with factory() as session:
            current_user = await session.get(User, user.id)

            organization = await create_organization(
                session,
                current_user,
                OrganizationCreateRequest(name="Suspended Member Test"),
            )

            await session.commit()

            membership = await session.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == organization.id,
                    OrganizationMember.user_id == user.id,
                )
            )

            membership.status = "suspended"
            await session.commit()

        async with factory() as session:
            current_user = await session.get(User, user.id)

            authorize = require_organization_roles("owner", "admin")

            with pytest.raises(HTTPException) as exc_info:
                await authorize(
                    organization_id=organization.id,
                    session=session,
                    current_user=current_user,
                )

            assert exc_info.value.status_code == 404
            assert exc_info.value.detail == "Organization not found."

    finally:
        await cleanup_test_data(user.id)
