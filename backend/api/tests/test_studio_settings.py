from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.engine import get_session_factory
from app.main import app
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User

client = TestClient(app)


def _user_and_token() -> tuple[dict, str]:
    email = f"studio-settings-{uuid4().hex}@example.com"
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Studio Settings Test",
            "password": "Aurora-Secure-Password-2026",
        },
    )
    assert registration.status_code == 201
    user_data = registration.json()

    import asyncio

    async def verify_user() -> None:
        async with get_session_factory()() as session:
            user = await session.get(User, user_data["id"])
            assert user is not None
            user.email_verified_at = datetime.now(UTC)
            await session.commit()

    asyncio.run(verify_user())
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Aurora-Secure-Password-2026"},
    )
    assert login.status_code == 200
    return user_data, login.json()["access_token"]


def test_workspace_settings_are_tenant_scoped_and_updateable_by_owner() -> None:
    user, token = _user_and_token()
    headers = {"Authorization": f"Bearer {token}"}
    organization_id = None
    try:
        created = client.post(
            "/api/v1/organizations",
            headers=headers,
            json={"name": f"Studio Settings {uuid4().hex}"},
        )
        assert created.status_code == 201
        organization_id = created.json()["id"]
        url = f"/api/v1/organizations/{organization_id}/studio-settings"

        defaults = client.get(url, headers=headers)
        assert defaults.status_code == 200
        assert defaults.json()["settings"]["research_enabled"] is True
        assert defaults.json()["settings"]["video_generation_enabled"] is True
        assert defaults.json()["settings"]["automatic_publishing_enabled"] is False
        assert defaults.json()["can_edit"] is True

        payload = {
            "research_enabled": False,
            "video_generation_enabled": True,
            "automatic_publishing_enabled": True,
            "target_duration_seconds": 45,
            "qwen_thinking_mode": "on",
            "story_max_output_tokens": 2048,
            "story_timeout_seconds": 300,
            "language": "Hindi",
            "tone": "Warm, explanatory",
            "audience": "New learners",
            "additional_rules": ["Use metric units", "Avoid sensational claims"],
        }
        saved = client.put(url, headers=headers, json=payload)
        assert saved.status_code == 200
        assert saved.json()["settings"] == payload
        assert saved.json()["updated_by_user_id"] == user["id"]

        paused = client.post(
            f"/api/v1/organizations/{organization_id}/research-runs",
            headers=headers,
            json={"topic": "A test topic"},
        )
        assert paused.status_code == 409
        assert "paused" in paused.json()["detail"]

        rejected_secret = client.put(
            url,
            headers=headers,
            json={**payload, "api_key": "must-never-be-accepted"},
        )
        assert rejected_secret.status_code == 422
    finally:
        if organization_id is not None:

            async def cleanup() -> None:
                async with get_session_factory()() as session:
                    async with session.begin():
                        await session.execute(
                            delete(OrganizationMember).where(
                                OrganizationMember.organization_id == organization_id
                            )
                        )
                        await session.execute(
                            delete(Organization).where(Organization.id == organization_id)
                        )
                        await session.execute(delete(User).where(User.id == user["id"]))

            import asyncio

            asyncio.run(cleanup())
