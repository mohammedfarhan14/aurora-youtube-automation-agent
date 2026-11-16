from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.engine import get_session_factory
from app.main import app
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User

client = TestClient(app)



def create_authenticated_user(verified: bool = True):
    email = f"organization-api-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Organization API Test",
            "password": password,
        },
    )

    assert registration.status_code == 201

    user_id = registration.json()["id"]

    if verified:
        import asyncio

        async def verify_test_user():
            async with get_session_factory()() as session:
                user = await session.get(User, user_id)

                assert user is not None

                user.email_verified_at = datetime.now(UTC)
                await session.commit()

        asyncio.run(verify_test_user())

    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "password": password,
        },
    )

    assert login.status_code == 200

    return registration.json(), login.json()["access_token"]



def cleanup_user_and_organizations(user_id):
    async def cleanup():
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    delete(OrganizationMember).where(OrganizationMember.user_id == user_id)
                )

                await session.execute(
                    delete(Organization).where(Organization.created_by_user_id == user_id)
                )

                await session.execute(delete(User).where(User.id == user_id))

    import asyncio

    asyncio.run(cleanup())


def test_create_organization_api_returns_201_and_assigns_owner():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]

    try:
        response = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "Aurora API Test"},
        )

        assert response.status_code == 201

        organization_data = response.json()

        assert organization_data["name"] == "Aurora API Test"
        assert organization_data["slug"] == "aurora-api-test"
        assert organization_data["plan"] == "free"
        assert organization_data["status"] == "active"

        async def verify_owner_membership():
            async with get_session_factory()() as session:
                membership = await session.scalar(
                    select(OrganizationMember).where(
                        OrganizationMember.organization_id == organization_data["id"],
                        OrganizationMember.user_id == user_id,
                    )
                )

                assert membership is not None
                assert membership.role == "owner"
                assert membership.status == "active"

        import asyncio

        asyncio.run(verify_owner_membership())

    finally:
        cleanup_user_and_organizations(user_id)


def test_create_organization_api_rejects_missing_token():
    response = client.post(
        "/api/v1/organizations",
        json={"name": "Unauthorized Organization"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials."
    assert response.headers["www-authenticate"] == "Bearer"


def test_unverified_user_can_login_but_cannot_access_organizations():
    user_data, token = create_authenticated_user(verified=False)
    user_id = user_data["id"]
    headers = {"Authorization": f"Bearer {token}"}

    try:
        endpoints = [
            ("post", "/api/v1/organizations", {"name": "Unverified Org"}),
            ("get", "/api/v1/organizations", None),
            ("get", f"/api/v1/organizations/{uuid4()}", None),
        ]

        for method, url, payload in endpoints:
            if method == "post":
                response = client.post(
                    url,
                    headers=headers,
                    json=payload,
                )
            else:
                response = client.get(
                    url,
                    headers=headers,
                )

            assert response.status_code == 403
            assert response.json()["detail"] == (
                "Email verification is required to access this resource."
            )

    finally:
        cleanup_user_and_organizations(user_id)


def test_unverified_user_cannot_update_organization():
    owner_data, owner_token = create_authenticated_user()
    unverified_data, unverified_token = create_authenticated_user(
        verified=False
    )

    owner_id = owner_data["id"]
    unverified_id = unverified_data["id"]

    try:
        creation = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Verification Protected Organization"},
        )

        assert creation.status_code == 201
        organization_id = creation.json()["id"]

        response = client.patch(
            f"/api/v1/organizations/{organization_id}",
            headers={"Authorization": f"Bearer {unverified_token}"},
            json={"name": "Unauthorized Rename"},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == (
            "Email verification is required to access this resource."
        )

    finally:
        cleanup_user_and_organizations(unverified_id)
        cleanup_user_and_organizations(owner_id)



def test_create_organization_api_rejects_invalid_names():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]

    try:
        invalid_names = ["", "   "]

        for name in invalid_names:
            response = client.post(
                "/api/v1/organizations",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": name},
            )

            assert response.status_code == 422

    finally:
        cleanup_user_and_organizations(user_id)


def test_list_organizations_api_enforces_tenant_isolation():
    user_a_data, token_a = create_authenticated_user()
    user_b_data, token_b = create_authenticated_user()

    user_a_id = user_a_data["id"]
    user_b_id = user_b_data["id"]

    try:
        organization_a = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"name": f"User A Organization {uuid4().hex[:8]}"},
        )

        organization_b = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"name": f"User B Organization {uuid4().hex[:8]}"},
        )

        assert organization_a.status_code == 201
        assert organization_b.status_code == 201

        organization_a_id = organization_a.json()["id"]
        organization_b_id = organization_b.json()["id"]

        response_a = client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token_a}"},
        )

        response_b = client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token_b}"},
        )

        assert response_a.status_code == 200
        assert response_b.status_code == 200

        user_a_organization_ids = {organization["id"] for organization in response_a.json()}

        user_b_organization_ids = {organization["id"] for organization in response_b.json()}

        assert organization_a_id in user_a_organization_ids
        assert organization_b_id not in user_a_organization_ids

        assert organization_b_id in user_b_organization_ids
        assert organization_a_id not in user_b_organization_ids

    finally:
        cleanup_user_and_organizations(user_a_id)
        cleanup_user_and_organizations(user_b_id)


def test_list_organizations_api_returns_empty_list_for_new_user():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]

    try:
        response = client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        assert response.json() == []

    finally:
        cleanup_user_and_organizations(user_id)


def test_get_organization_api_returns_organization_to_member():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]

    try:
        creation = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": f"Retrieval Test {uuid4().hex[:8]}"},
        )

        assert creation.status_code == 201
        organization_id = creation.json()["id"]

        response = client.get(
            f"/api/v1/organizations/{organization_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        assert response.json()["id"] == organization_id

    finally:
        cleanup_user_and_organizations(user_id)


def test_get_organization_api_rejects_non_member():
    owner_data, owner_token = create_authenticated_user()
    requester_data, requester_token = create_authenticated_user()

    owner_id = owner_data["id"]
    requester_id = requester_data["id"]

    try:
        creation = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": f"Private Organization {uuid4().hex[:8]}"},
        )

        assert creation.status_code == 201
        organization_id = creation.json()["id"]

        response = client.get(
            f"/api/v1/organizations/{organization_id}",
            headers={"Authorization": f"Bearer {requester_token}"},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Organization not found."

    finally:
        cleanup_user_and_organizations(requester_id)
        cleanup_user_and_organizations(owner_id)


def test_get_organization_api_returns_404_for_nonexistent_organization():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]

    try:
        nonexistent_id = str(uuid4())

        response = client.get(
            f"/api/v1/organizations/{nonexistent_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Organization not found."

    finally:
        cleanup_user_and_organizations(user_id)


def test_get_organization_api_rejects_invalid_uuid():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]

    try:
        response = client.get(
            "/api/v1/organizations/not-a-valid-uuid",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 422

    finally:
        cleanup_user_and_organizations(user_id)


def test_update_organization_api_allows_owner_to_rename():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]

    try:
        creation = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "Original Organization"},
        )

        assert creation.status_code == 201

        original = creation.json()
        organization_id = original["id"]
        original_slug = original["slug"]

        response = client.patch(
            f"/api/v1/organizations/{organization_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "Updated Organization"},
        )

        assert response.status_code == 200

        updated = response.json()

        assert updated["id"] == organization_id
        assert updated["name"] == "Updated Organization"
        assert updated["slug"] == original_slug
        assert updated["plan"] == "free"
        assert updated["status"] == "active"

    finally:
        cleanup_user_and_organizations(user_id)


def test_update_organization_api_allows_admin_to_rename():
    owner_data, owner_token = create_authenticated_user()
    admin_data, admin_token = create_authenticated_user()

    owner_id = owner_data["id"]
    admin_id = admin_data["id"]

    try:
        creation = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Admin Test Organization"},
        )

        assert creation.status_code == 201

        organization_id = creation.json()["id"]

        import asyncio

        async def assign_admin_role():
            async with get_session_factory()() as session:
                membership = await session.scalar(
                    select(OrganizationMember).where(
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.user_id == admin_id,
                    )
                )

                if membership is None:
                    membership = OrganizationMember(
                        organization_id=organization_id,
                        user_id=admin_id,
                        role="admin",
                        status="active",
                    )
                    session.add(membership)
                else:
                    membership.role = "admin"

                await session.commit()

        asyncio.run(assign_admin_role())

        response = client.patch(
            f"/api/v1/organizations/{organization_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Renamed By Admin"},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Renamed By Admin"

    finally:
        cleanup_user_and_organizations(admin_id)
        cleanup_user_and_organizations(owner_id)


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_update_organization_api_rejects_editor_and_viewer(role):
    owner_data, owner_token = create_authenticated_user()
    member_data, member_token = create_authenticated_user()

    owner_id = owner_data["id"]
    member_id = member_data["id"]

    try:
        creation = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Restricted Update Organization"},
        )

        assert creation.status_code == 201
        organization_id = creation.json()["id"]

        import asyncio

        async def assign_role():
            async with get_session_factory()() as session:
                membership = OrganizationMember(
                    organization_id=organization_id,
                    user_id=member_id,
                    role=role,
                    status="active",
                )
                session.add(membership)
                await session.commit()

        asyncio.run(assign_role())

        response = client.patch(
            f"/api/v1/organizations/{organization_id}",
            headers={"Authorization": f"Bearer {member_token}"},
            json={"name": "Unauthorized Rename"},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Insufficient organization permissions."

    finally:
        cleanup_user_and_organizations(member_id)
        cleanup_user_and_organizations(owner_id)


def test_update_organization_api_rejects_non_member():
    owner_data, owner_token = create_authenticated_user()
    outsider_data, outsider_token = create_authenticated_user()

    owner_id = owner_data["id"]
    outsider_id = outsider_data["id"]

    try:
        creation = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Private Organization"},
        )

        assert creation.status_code == 201
        organization_id = creation.json()["id"]

        response = client.patch(
            f"/api/v1/organizations/{organization_id}",
            headers={"Authorization": f"Bearer {outsider_token}"},
            json={"name": "Unauthorized Rename"},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Organization not found."

    finally:
        cleanup_user_and_organizations(outsider_id)
        cleanup_user_and_organizations(owner_id)


def test_update_organization_api_rejects_invalid_payloads():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]

    try:
        creation = client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "Protected Fields Organization"},
        )

        assert creation.status_code == 201

        original = creation.json()
        organization_id = original["id"]

        invalid_payloads = [
            {},
            {"name": ""},
            {"name": "   "},
            {"name": "Valid Name", "slug": "changed-slug"},
            {"name": "Valid Name", "plan": "enterprise"},
            {"name": "Valid Name", "status": "suspended"},
        ]

        for payload in invalid_payloads:
            response = client.patch(
                f"/api/v1/organizations/{organization_id}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )

            assert response.status_code == 422, (
                f"Expected 422 for payload {payload}, "
                f"received {response.status_code}: {response.text}"
            )

        verification = client.get(
            f"/api/v1/organizations/{organization_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert verification.status_code == 200
        assert verification.json()["name"] == original["name"]
        assert verification.json()["slug"] == original["slug"]
        assert verification.json()["plan"] == original["plan"]
        assert verification.json()["status"] == original["status"]

    finally:
        cleanup_user_and_organizations(user_id)
