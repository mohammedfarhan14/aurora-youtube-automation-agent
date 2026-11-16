import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import Settings
from app.database.engine import get_session_factory
from app.main import app
from app.models.email_outbox import EmailOutbox
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User
from app.services.operations_alerts import enqueue_operations_alert

client = TestClient(app)


def create_user_and_organization() -> tuple[str, str]:
    email = f"operations-alert-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Operations Alert Test",
            "password": password,
        },
    )
    assert registration.status_code == 201
    user_id = registration.json()["id"]

    async def verify_user():
        async with get_session_factory()() as session:
            user = await session.get(User, UUID(user_id))
            assert user is not None
            user.email_verified_at = datetime.now(UTC)
            await session.commit()

    asyncio.run(verify_user())
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200
    organization = client.post(
        "/api/v1/organizations",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={"name": f"Operations Alert {uuid4().hex[:8]}"},
    )
    assert organization.status_code == 201
    return user_id, organization.json()["id"]


def cleanup_user(user_id: str) -> None:
    async def cleanup():
        async with get_session_factory()() as session:
            async with session.begin():
                org_ids = select(Organization.id).where(
                    Organization.created_by_user_id == UUID(user_id)
                )
                await session.execute(
                    delete(EmailOutbox).where(EmailOutbox.user_id == UUID(user_id))
                )
                await session.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.user_id == UUID(user_id)
                    )
                )
                await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))
                await session.execute(delete(User).where(User.id == UUID(user_id)))

    asyncio.run(cleanup())


def test_terminal_failure_alert_uses_existing_encrypted_email_outbox():
    user_id, organization_id = create_user_and_organization()
    settings = Settings(
        _env_file=None,
        operations_alerts_enabled=True,
        email_outbox_encryption_key="a" * 64,
    )
    try:
        queued = asyncio.run(
            enqueue_operations_alert(
                get_session_factory(),
                organization_id=UUID(organization_id),
                subject="Aurora Studio: worker failed",
                body="Run ID: test-run\nFailure code: stage_lease_expired",
                settings=settings,
            )
        )
        assert queued is True

        async def inspect():
            async with get_session_factory()() as session:
                alert = await session.scalar(
                    select(EmailOutbox).where(
                        EmailOutbox.user_id == UUID(user_id),
                        EmailOutbox.message_type == "operations_alert",
                    )
                )
                assert alert is not None
                assert alert.recipient.endswith("@example.com")
                assert alert.status == "pending"
                assert "stage_lease_expired" not in alert.encrypted_payload

        asyncio.run(inspect())
    finally:
        cleanup_user(user_id)


def test_operations_alerts_are_disabled_by_default():
    _, organization_id = create_user_and_organization()
    try:
        queued = asyncio.run(
            enqueue_operations_alert(
                get_session_factory(),
                organization_id=UUID(organization_id),
                subject="Should not queue",
                body="Disabled.",
                settings=Settings(_env_file=None),
            )
        )
        assert queued is False
    finally:
        # Find and clean the test user through its created organization.
        async def get_creator():
            async with get_session_factory()() as session:
                organization = await session.scalar(
                    select(Organization).where(
                        Organization.id == UUID(organization_id)
                    )
                )
                assert organization is not None
                return str(organization.created_by_user_id)

        cleanup_user(asyncio.run(get_creator()))
