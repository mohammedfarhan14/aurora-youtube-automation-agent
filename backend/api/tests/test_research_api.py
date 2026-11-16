import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.api.v1.endpoints import research as research_endpoint
from app.database.engine import get_session_factory
from app.main import app
from app.models.evidence_item import EvidenceItem
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.organization_source_rights_profile import OrganizationSourceRightsProfile
from app.models.research_run import ResearchRun
from app.models.research_source import ResearchSource
from app.models.research_source_rights_review import ResearchSourceRightsReview
from app.models.user import User
from app.research.normalization import content_sha256

client = TestClient(app)


def create_authenticated_user():
    email = f"research-api-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Research API Test",
            "password": password,
        },
    )
    assert registration.status_code == 201

    user_id = registration.json()["id"]

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

    return registration.json(), login.json()["access_token"]


def create_organization(token: str) -> str:
    response = client.post(
        "/api/v1/organizations",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"Research Test {uuid4().hex[:8]}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def cleanup_user_and_research(user_id: str) -> None:
    async def cleanup():
        async with get_session_factory()() as session:
            async with session.begin():
                organization_ids = select(Organization.id).where(
                    Organization.created_by_user_id == user_id
                )

                await session.execute(
                    delete(ResearchRun).where(
                        ResearchRun.organization_id.in_(organization_ids)
                    )
                )
                await session.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.user_id == user_id
                    )
                )
                await session.execute(
                    delete(Organization).where(
                        Organization.created_by_user_id == user_id
                    )
                )
                await session.execute(
                    delete(User).where(User.id == user_id)
                )

    asyncio.run(cleanup())


def test_create_research_run_requires_authentication():
    response = client.post(
        f"/api/v1/organizations/{uuid4()}/research-runs",
        json={"topic": "Renewable energy storage"},
    )

    assert response.status_code == 401


def test_create_research_run_persists_organization_and_requester():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]
    organization_id = create_organization(token)

    try:
        response = client.post(
            f"/api/v1/organizations/{organization_id}/research-runs",
            headers={"Authorization": f"Bearer {token}"},
            json={"topic": "Renewable energy storage"},
        )

        assert response.status_code == 201
        result = response.json()

        assert result["organization_id"] == organization_id
        assert result["requested_by_user_id"] == user_id
        assert result["topic"] == "Renewable energy storage"
        assert result["status"] == "queued"
        assert result["failure_code"] is None
        assert result["failure_message"] is None

        async def verify_persistence():
            async with get_session_factory()() as session:
                research_run = await session.get(ResearchRun, result["id"])
                assert research_run is not None
                assert str(research_run.organization_id) == organization_id
                assert str(research_run.requested_by_user_id) == user_id

        asyncio.run(verify_persistence())

    finally:
        cleanup_user_and_research(user_id)


def test_create_research_run_rejects_non_member():
    owner_data, owner_token = create_authenticated_user()
    outsider_data, outsider_token = create_authenticated_user()

    owner_id = owner_data["id"]
    outsider_id = outsider_data["id"]
    organization_id = create_organization(owner_token)

    try:
        response = client.post(
            f"/api/v1/organizations/{organization_id}/research-runs",
            headers={"Authorization": f"Bearer {outsider_token}"},
            json={"topic": "Renewable energy storage"},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Organization not found."

    finally:
        cleanup_user_and_research(outsider_id)
        cleanup_user_and_research(owner_id)


def test_create_research_run_rejects_viewer():
    owner_data, owner_token = create_authenticated_user()
    viewer_data, viewer_token = create_authenticated_user()

    owner_id = owner_data["id"]
    viewer_id = viewer_data["id"]
    organization_id = create_organization(owner_token)

    async def assign_viewer():
        async with get_session_factory()() as session:
            session.add(
                OrganizationMember(
                    organization_id=UUID(organization_id),
                    user_id=UUID(viewer_id),
                    role="viewer",
                    status="active",
                )
            )
            await session.commit()

    try:
        asyncio.run(assign_viewer())

        response = client.post(
            f"/api/v1/organizations/{organization_id}/research-runs",
            headers={"Authorization": f"Bearer {viewer_token}"},
            json={"topic": "Renewable energy storage"},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Insufficient organization permissions."

    finally:
        cleanup_user_and_research(viewer_id)
        cleanup_user_and_research(owner_id)


def test_create_research_run_rejects_invalid_topics():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]
    organization_id = create_organization(token)

    try:
        for topic in ["", "   ", "x" * 301]:
            response = client.post(
                f"/api/v1/organizations/{organization_id}/research-runs",
                headers={"Authorization": f"Bearer {token}"},
                json={"topic": topic},
            )
            assert response.status_code == 422

    finally:
        cleanup_user_and_research(user_id)


def test_create_research_run_is_rate_limited_before_enqueue(monkeypatch):
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]
    organization_id = create_organization(token)
    observed_limits = []

    async def reject_request(request, *limits):
        observed_limits.extend(limits)
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": "60"},
        )

    monkeypatch.setattr(research_endpoint, "enforce_rate_limits", reject_request)

    try:
        response = client.post(
            f"/api/v1/organizations/{organization_id}/research-runs",
            headers={"Authorization": f"Bearer {token}"},
            json={"topic": "Renewable energy storage"},
        )

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert [limit.scope for limit in observed_limits] == [
            "research-run-user",
            "research-run-organization",
        ]
        assert [limit.identity for limit in observed_limits] == [
            user_id,
            organization_id,
        ]
        assert [limit.limit for limit in observed_limits] == [20, 100]
        assert all(limit.window_seconds == 3600 for limit in observed_limits)

        async def verify_no_run_was_created():
            async with get_session_factory()() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(ResearchRun)
                    .where(ResearchRun.organization_id == organization_id)
                )
                assert count == 0

        asyncio.run(verify_no_run_was_created())

    finally:
        cleanup_user_and_research(user_id)



def test_list_research_runs_is_tenant_scoped_and_paginated():
    owner_data, owner_token = create_authenticated_user()
    owner_id = owner_data["id"]
    organization_id = create_organization(owner_token)
    other_org_id = create_organization(owner_token)

    async def seed_runs():
        async with get_session_factory()() as session:
            session.add_all(
                [
                    ResearchRun(
                        organization_id=UUID(organization_id),
                        requested_by_user_id=UUID(owner_id),
                        topic=f"Tenant run {index}",
                        status="queued",
                    )
                    for index in range(3)
                ]
                + [
                    ResearchRun(
                        organization_id=UUID(other_org_id),
                        requested_by_user_id=UUID(owner_id),
                        topic="Must not leak",
                        status="queued",
                    )
                ]
            )
            await session.commit()

    try:
        asyncio.run(seed_runs())
        url = f"/api/v1/organizations/{organization_id}/research-runs"
        headers = {"Authorization": f"Bearer {owner_token}"}

        first_page = client.get(url, headers=headers, params={"limit": 2, "offset": 0})
        second_page = client.get(url, headers=headers, params={"limit": 2, "offset": 2})

        assert first_page.status_code == 200
        assert second_page.status_code == 200
        assert len(first_page.json()) == 2
        assert len(second_page.json()) == 1
        assert all(item["organization_id"] == organization_id for item in first_page.json())
        assert all(item["organization_id"] == organization_id for item in second_page.json())
        assert "Must not leak" not in str(first_page.json() + second_page.json())

    finally:
        cleanup_user_and_research(owner_id)


def test_get_research_run_returns_status_for_organization_member():
    owner_data, owner_token = create_authenticated_user()
    viewer_data, viewer_token = create_authenticated_user()
    owner_id = owner_data["id"]
    viewer_id = viewer_data["id"]
    organization_id = create_organization(owner_token)

    async def seed_viewer_and_run():
        async with get_session_factory()() as session:
            session.add(
                OrganizationMember(
                    organization_id=UUID(organization_id),
                    user_id=UUID(viewer_id),
                    role="viewer",
                    status="active",
                )
            )
            run = ResearchRun(
                organization_id=UUID(organization_id),
                requested_by_user_id=UUID(owner_id),
                topic="Grid-scale battery storage",
                status="researching",
            )
            session.add(run)
            await session.commit()
            return str(run.id)

    try:
        run_id = asyncio.run(seed_viewer_and_run())
        response = client.get(
            f"/api/v1/organizations/{organization_id}/research-runs/{run_id}",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )

        assert response.status_code == 200
        assert response.json()["id"] == run_id
        assert response.json()["status"] == "researching"
        assert response.json()["topic"] == "Grid-scale battery storage"

    finally:
        cleanup_user_and_research(viewer_id)
        cleanup_user_and_research(owner_id)


def test_get_research_run_does_not_disclose_other_organization_runs():
    owner_data, owner_token = create_authenticated_user()
    outsider_data, outsider_token = create_authenticated_user()
    owner_id = owner_data["id"]
    outsider_id = outsider_data["id"]
    organization_id = create_organization(owner_token)

    async def seed_run():
        async with get_session_factory()() as session:
            run = ResearchRun(
                organization_id=UUID(organization_id),
                requested_by_user_id=UUID(owner_id),
                topic="Private tenant research",
                status="queued",
            )
            session.add(run)
            await session.commit()
            return str(run.id)

    try:
        run_id = asyncio.run(seed_run())
        response = client.get(
            f"/api/v1/organizations/{organization_id}/research-runs/{run_id}",
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Organization not found."

    finally:
        cleanup_user_and_research(outsider_id)
        cleanup_user_and_research(owner_id)



def test_list_research_sources_and_evidence_is_tenant_scoped_and_paginated():
    owner_data, owner_token = create_authenticated_user()
    viewer_data, viewer_token = create_authenticated_user()
    owner_id = owner_data["id"]
    viewer_id = viewer_data["id"]
    organization_id = create_organization(owner_token)

    async def seed_data():
        async with get_session_factory()() as session:
            session.add(
                OrganizationMember(
                    organization_id=UUID(organization_id),
                    user_id=UUID(viewer_id),
                    role="viewer",
                    status="active",
                )
            )
            run = ResearchRun(
                organization_id=UUID(organization_id),
                requested_by_user_id=UUID(owner_id),
                topic="Grid-scale battery storage",
                status="completed",
            )
            session.add(run)
            await session.flush()
            sources = [
                ResearchSource(
                    research_run_id=run.id,
                    canonical_url=f"https://example.com/source-{index}",
                    title=f"Source {index}",
                    publisher="Example Publisher",
                    source_type="webpage",
                    retrieved_at=datetime.now(UTC),
                    content_hash=f"{index:064x}",
                )
                for index in range(3)
            ]
            session.add_all(sources)
            await session.flush()
            session.add_all(
                [
                    EvidenceItem(
                        research_source_id=sources[0].id,
                        excerpt=f"Evidence excerpt {index}",
                        content_hash=f"{index + 10:064x}",
                        locator=f"Section {index}",
                    )
                    for index in range(3)
                ]
            )
            await session.commit()
            return str(run.id), [str(source.id) for source in sources]

    try:
        run_id, source_ids = asyncio.run(seed_data())
        base_url = (
            f"/api/v1/organizations/{organization_id}/research-runs/{run_id}"
        )
        headers = {"Authorization": f"Bearer {viewer_token}"}

        first_page = client.get(
            f"{base_url}/sources",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        second_page = client.get(
            f"{base_url}/sources",
            headers=headers,
            params={"limit": 2, "offset": 2},
        )
        repeated_first_page = client.get(
            f"{base_url}/sources",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )

        assert first_page.status_code == 200
        assert second_page.status_code == 200
        assert len(first_page.json()) == 2
        assert len(second_page.json()) == 1
        assert [item["id"] for item in first_page.json()] == [
            item["id"] for item in repeated_first_page.json()
        ]
        assert {item["id"] for item in first_page.json() + second_page.json()} == set(
            source_ids
        )
        assert all("raw_content" not in item for item in first_page.json())
        assert first_page.json()[0]["canonical_url"].startswith("https://example.com/")
        assert all(item["source_class"] == "unknown" for item in first_page.json())
        assert all(item["independence_basis"] == "unassessed" for item in first_page.json())
        assert all(item["classification_version"] == "0" for item in first_page.json())
        assert all(item["rights_metadata"]["status"] == "unknown" for item in first_page.json())
        assert all(item["rights_metadata"]["scope"] == "unknown" for item in first_page.json())

        evidence_response = client.get(
            f"{base_url}/sources/{source_ids[0]}/evidence",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        evidence_second_page = client.get(
            f"{base_url}/sources/{source_ids[0]}/evidence",
            headers=headers,
            params={"limit": 2, "offset": 2},
        )
        evidence_repeat = client.get(
            f"{base_url}/sources/{source_ids[0]}/evidence",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        assert evidence_response.status_code == 200
        assert evidence_second_page.status_code == 200
        assert len(evidence_response.json()) == 2
        assert len(evidence_second_page.json()) == 1
        assert [item["id"] for item in evidence_response.json()] == [
            item["id"] for item in evidence_repeat.json()
        ]
        assert evidence_response.json()[0]["excerpt"].startswith("Evidence excerpt")
        assert evidence_response.json()[0]["locator"] is not None

    finally:
        cleanup_user_and_research(viewer_id)
        cleanup_user_and_research(owner_id)


def test_research_evidence_rejects_source_outside_requested_run():
    owner_data, owner_token = create_authenticated_user()
    owner_id = owner_data["id"]
    organization_id = create_organization(owner_token)

    async def seed_two_runs():
        async with get_session_factory()() as session:
            runs = [
                ResearchRun(
                    organization_id=UUID(organization_id),
                    requested_by_user_id=UUID(owner_id),
                    topic=f"Research topic {index}",
                    status="completed",
                )
                for index in range(2)
            ]
            session.add_all(runs)
            await session.flush()
            source = ResearchSource(
                research_run_id=runs[1].id,
                canonical_url="https://example.com/private-source",
                title="Private source",
                publisher="Example Publisher",
                source_type="webpage",
                retrieved_at=datetime.now(UTC),
            )
            session.add(source)
            await session.commit()
            return str(runs[0].id), str(source.id)

    try:
        run_id, source_id = asyncio.run(seed_two_runs())
        response = client.get(
            f"/api/v1/organizations/{organization_id}/research-runs/{run_id}"
            f"/sources/{source_id}/evidence",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Research source not found."

    finally:
        cleanup_user_and_research(owner_id)



def test_research_sources_and_evidence_do_not_disclose_another_tenant():
    owner_data, owner_token = create_authenticated_user()
    outsider_data, outsider_token = create_authenticated_user()
    owner_id = owner_data["id"]
    outsider_id = outsider_data["id"]
    organization_id = create_organization(owner_token)

    async def seed_run_and_source():
        async with get_session_factory()() as session:
            run = ResearchRun(
                organization_id=UUID(organization_id),
                requested_by_user_id=UUID(owner_id),
                topic="Tenant-private research",
                status="completed",
            )
            session.add(run)
            await session.flush()
            source = ResearchSource(
                research_run_id=run.id,
                canonical_url="https://example.com/tenant-private",
                title="Tenant-private source",
                publisher="Example Publisher",
                source_type="webpage",
                retrieved_at=datetime.now(UTC),
            )
            session.add(source)
            await session.flush()
            session.add(
                EvidenceItem(
                    research_source_id=source.id,
                    excerpt="Tenant-private evidence excerpt",
                    content_hash="a" * 64,
                )
            )
            await session.commit()
            return str(run.id), str(source.id)

    try:
        run_id, source_id = asyncio.run(seed_run_and_source())
        headers = {"Authorization": f"Bearer {outsider_token}"}
        sources_response = client.get(
            f"/api/v1/organizations/{organization_id}/research-runs/{run_id}/sources",
            headers=headers,
        )
        evidence_response = client.get(
            f"/api/v1/organizations/{organization_id}/research-runs/{run_id}"
            f"/sources/{source_id}/evidence",
            headers=headers,
        )
        assert sources_response.status_code == 404
        assert evidence_response.status_code == 404
        assert sources_response.json()["detail"] == "Organization not found."
        assert evidence_response.json()["detail"] == "Organization not found."

    finally:
        cleanup_user_and_research(outsider_id)
        cleanup_user_and_research(owner_id)


def create_research_source_for_rights_review(
    *,
    user_id: str,
    organization_id: str,
) -> tuple[str, str]:
    async def seed():
        async with get_session_factory()() as session:
            research_run = ResearchRun(
                organization_id=UUID(organization_id),
                requested_by_user_id=UUID(user_id),
                topic="Rights review test topic",
                status="completed",
            )
            session.add(research_run)
            await session.flush()
            canonical_url = f"https://example.com/source/{uuid4().hex}"
            source_revision_url = f"{canonical_url}/revision/1"
            source = ResearchSource(
                research_run_id=research_run.id,
                canonical_url=canonical_url,
                title="Rights review test source",
                publisher="Example Research",
                source_type="webpage",
                retrieved_at=datetime.now(UTC),
                rights_metadata={
                    "status": "review_required",
                    "scope": "source_text",
                    "license_ids": [],
                    "license_urls": [],
                    "rights_holder": None,
                    "creators": [],
                    "source_revision_url": source_revision_url,
                    "attribution_text": None,
                    "reuse_restrictions": ["review_page_history"],
                },
            )
            session.add(source)
            await session.commit()
            return str(research_run.id), str(source.id)

    return asyncio.run(seed())


def test_source_rights_review_is_audited_and_requires_a_new_story_run():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]
    organization_id = create_organization(token)
    research_run_id, source_id = create_research_source_for_rights_review(
        user_id=user_id,
        organization_id=organization_id,
    )

    async def read_source_url():
        async with get_session_factory()() as session:
            source = await session.get(ResearchSource, UUID(source_id))
            assert source is not None
            return (
                source.canonical_url,
                source.rights_metadata["source_revision_url"],
            )

    canonical_url, source_revision_url = asyncio.run(read_source_url())
    url = (
        f"/api/v1/organizations/{organization_id}/research-runs/{research_run_id}"
        f"/sources/{source_id}/rights-reviews"
    )
    headers = {"Authorization": f"Bearer {token}"}

    cleared_payload = {
        "decision": "cleared",
        "scope": "source_text",
        "license_ids": ["CC0-1.0"],
        "license_urls": ["https://creativecommons.org/publicdomain/zero/1.0/"],
        "rights_holder": "Example Research",
        "creators": ["Example Research"],
        "source_revision_url": source_revision_url,
        "attribution_text": "Example Research, CC0 1.0",
        "reuse_restrictions": ["attribution"],
        "review_notes": "Verified the source page and the linked CC0 license.",
        "evidence_urls": [
            "https://example.com/source/rights",
            source_revision_url,
            "https://creativecommons.org/publicdomain/zero/1.0/",
        ],
    }

    try:
        cleared = client.post(url, headers=headers, json=cleared_payload)
        assert cleared.status_code == 201
        cleared_body = cleared.json()
        assert cleared_body["requires_new_story_run"] is True
        assert cleared_body["source"]["rights_metadata"]["status"] == "cleared"
        assert cleared_body["review"]["decision"] == "cleared"
        assert cleared_body["review"]["reviewer_user_id"] == user_id
        rights_profile_id = cleared_body["review"]["rights_profile_id"]
        assert cleared_body["review"]["previous_rights_metadata"]["status"] == "review_required"
        assert cleared_body["review"]["resulting_rights_metadata"]["status"] == "cleared"

        restricted = client.post(
            url,
            headers=headers,
            json={
                "decision": "restricted",
                "scope": "source_text",
                "source_revision_url": source_revision_url,
                "review_notes": "A later review found a conflicting reuse notice.",
                "evidence_urls": [
                    "https://example.com/source/terms",
                    source_revision_url,
                ],
            },
        )
        assert restricted.status_code == 201
        assert restricted.json()["source"]["rights_metadata"]["status"] == "restricted"
        assert restricted.json()["review"]["rights_profile_id"] == rights_profile_id
        assert (
            restricted.json()["review"]["previous_rights_metadata"]["status"]
            == "cleared"
        )

        history = client.get(url, headers=headers)
        assert history.status_code == 200
        history_items = history.json()
        assert [item["decision"] for item in history_items] == [
            "restricted",
            "cleared",
        ]

        async def verify_audit_and_source():
            async with get_session_factory()() as session:
                source = await session.get(ResearchSource, UUID(source_id))
                assert source is not None
                assert source.rights_metadata["status"] == "restricted"
                profile = await session.scalar(
                    select(OrganizationSourceRightsProfile).where(
                        OrganizationSourceRightsProfile.organization_id
                        == UUID(organization_id),
                        OrganizationSourceRightsProfile.canonical_url_hash
                        == content_sha256(source.canonical_url),
                    )
                )
                assert profile is not None
                assert profile.rights_metadata["status"] == "restricted"
                reviews = await session.scalars(
                    select(ResearchSourceRightsReview)
                    .where(ResearchSourceRightsReview.research_source_id == UUID(source_id))
                    .order_by(ResearchSourceRightsReview.created_at.desc())
                )
                assert len(reviews.all()) == 2

        asyncio.run(verify_audit_and_source())

        async def delete_research_run():
            async with get_session_factory()() as session:
                await session.execute(
                    delete(ResearchRun).where(
                        ResearchRun.id == UUID(research_run_id)
                    )
                )
                await session.commit()

        asyncio.run(delete_research_run())
        retained_history = client.get(url, headers=headers)
        assert retained_history.status_code == 200
        assert [item["decision"] for item in retained_history.json()] == [
            "restricted",
            "cleared",
        ]
        profile_history = client.get(
            (
                f"/api/v1/organizations/{organization_id}/source-rights-profiles/"
                f"{rights_profile_id}/reviews"
            ),
            headers=headers,
        )
        assert profile_history.status_code == 200
        assert [item["decision"] for item in profile_history.json()] == [
            "restricted",
            "cleared",
        ]

        async def verify_profile_survives_run_retention():
            async with get_session_factory()() as session:
                profile = await session.scalar(
                    select(OrganizationSourceRightsProfile).where(
                        OrganizationSourceRightsProfile.organization_id
                        == UUID(organization_id),
                        OrganizationSourceRightsProfile.canonical_url_hash
                        == content_sha256(canonical_url),
                    )
                )
                assert profile is not None
                assert profile.rights_metadata["status"] == "restricted"

        asyncio.run(verify_profile_survives_run_retention())
    finally:
        cleanup_user_and_research(user_id)


def test_source_rights_review_requires_owner_or_admin_and_is_tenant_scoped():
    owner_data, owner_token = create_authenticated_user()
    viewer_data, viewer_token = create_authenticated_user()
    owner_id = owner_data["id"]
    viewer_id = viewer_data["id"]
    organization_id = create_organization(owner_token)
    research_run_id, source_id = create_research_source_for_rights_review(
        user_id=owner_id,
        organization_id=organization_id,
    )

    async def assign_viewer():
        async with get_session_factory()() as session:
            session.add(
                OrganizationMember(
                    organization_id=UUID(organization_id),
                    user_id=UUID(viewer_id),
                    role="viewer",
                    status="active",
                )
            )
            await session.commit()

    asyncio.run(assign_viewer())
    url = (
        f"/api/v1/organizations/{organization_id}/research-runs/{research_run_id}"
        f"/sources/{source_id}/rights-reviews"
    )
    payload = {
        "decision": "cleared",
        "scope": "source_text",
        "license_ids": ["CC0-1.0"],
        "license_urls": ["https://creativecommons.org/publicdomain/zero/1.0/"],
        "attribution_text": "Example Research, CC0 1.0",
        "review_notes": "Verified the license.",
        "evidence_urls": [
            "https://example.com/source/rights",
            "https://creativecommons.org/publicdomain/zero/1.0/",
        ],
    }

    try:
        denied = client.post(
            url,
            headers={"Authorization": f"Bearer {viewer_token}"},
            json=payload,
        )
        assert denied.status_code == 403

        wrong_tenant = client.post(
            (
                f"/api/v1/organizations/{uuid4()}/research-runs/{research_run_id}"
                f"/sources/{source_id}/rights-reviews"
            ),
            headers={"Authorization": f"Bearer {owner_token}"},
            json=payload,
        )
        assert wrong_tenant.status_code == 404
    finally:
        cleanup_user_and_research(viewer_id)
        cleanup_user_and_research(owner_id)


def test_source_rights_review_rejects_incomplete_cleared_decision():
    user_data, token = create_authenticated_user()
    user_id = user_data["id"]
    organization_id = create_organization(token)
    research_run_id, source_id = create_research_source_for_rights_review(
        user_id=user_id,
        organization_id=organization_id,
    )

    async def read_current_revision():
        async with get_session_factory()() as session:
            source = await session.get(ResearchSource, UUID(source_id))
            assert source is not None
            return source.rights_metadata["source_revision_url"]

    current_revision_url = asyncio.run(read_current_revision())
    wrong_revision_url = current_revision_url.rsplit("/", 1)[0] + "/2"
    url = (
        f"/api/v1/organizations/{organization_id}/research-runs/{research_run_id}"
        f"/sources/{source_id}/rights-reviews"
    )

    try:
        response = client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "decision": "cleared",
                "scope": "source_text",
                "review_notes": "Attempted to clear rights without license evidence.",
                "evidence_urls": ["https://example.com/source/rights"],
            },
        )
        assert response.status_code == 422

        wrong_revision = client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "decision": "cleared",
                "scope": "source_text",
                "license_ids": ["CC0-1.0"],
                "license_urls": [
                    "https://creativecommons.org/publicdomain/zero/1.0/"
                ],
                "attribution_text": "Example Research, CC0 1.0",
                "source_revision_url": wrong_revision_url,
                "review_notes": "This evidence is for a different source revision.",
                "evidence_urls": [
                    "https://creativecommons.org/publicdomain/zero/1.0/",
                    wrong_revision_url,
                ],
            },
        )
        assert wrong_revision.status_code == 409

        invalid_evidence = client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "decision": "restricted",
                "review_notes": "Evidence URL must use HTTPS.",
                "evidence_urls": ["http://example.com/source/terms"],
            },
        )
        assert invalid_evidence.status_code == 422
    finally:
        cleanup_user_and_research(user_id)
