import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from app.database.engine import get_session_factory
from app.main import app
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.tool_execution_audit import ToolExecutionAuditRecord
from app.models.user import User
from app.skills import ToolExecutionAuditEvent, database_tool_audit_sink

client = TestClient(app)


def create_test_context() -> tuple[str, str]:
    email = f"skill-audit-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Skill Audit Test",
            "password": password,
        },
    )
    assert registration.status_code == 201
    user_id = registration.json()["id"]

    async def verify_user() -> None:
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
    organization = client.post(
        "/api/v1/organizations",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={"name": f"Skill Audit {uuid4().hex[:8]}"},
    )
    assert organization.status_code == 201
    return user_id, organization.json()["id"]


def cleanup_test_context(user_id: str) -> None:
    async def cleanup() -> None:
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    delete(OrganizationMember).where(OrganizationMember.user_id == user_id)
                )
                await session.execute(
                    delete(Organization).where(Organization.created_by_user_id == user_id)
                )
                await session.execute(delete(User).where(User.id == user_id))

    asyncio.run(cleanup())


def test_database_audit_sink_persists_tenant_scoped_immutable_event():
    user_id, organization_id = create_test_context()
    try:
        event = ToolExecutionAuditEvent(
            organization_id=UUID(organization_id),
            actor_id=UUID(user_id),
            request_id="audit-request-001",
            skill_id="research.topic_discovery",
            skill_version="1.0.0",
            tool_id="research.search",
            tool_version="1.0.0",
            input_sha256="a" * 64,
            output_sha256="b" * 64,
            outcome="success",
            failure_code=None,
            duration_ms=42,
        )

        async def persist_and_read() -> UUID:
            await database_tool_audit_sink(get_session_factory())(event)
            async with get_session_factory()() as session:
                record = await session.scalar(
                    select(ToolExecutionAuditRecord).where(
                        ToolExecutionAuditRecord.organization_id == UUID(organization_id),
                        ToolExecutionAuditRecord.request_id == event.request_id,
                    )
                )
                assert record is not None
                assert record.actor_id == UUID(user_id)
                assert record.skill_id == event.skill_id
                assert record.tool_id == event.tool_id
                assert record.input_sha256 == event.input_sha256
                assert record.output_sha256 == event.output_sha256
                assert record.outcome == "success"
                return record.id

        event_id = asyncio.run(persist_and_read())

        async def reject_mutation() -> None:
            async with get_session_factory()() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(
                        update(ToolExecutionAuditRecord)
                        .where(ToolExecutionAuditRecord.id == event_id)
                        .values(outcome="failure")
                    )
                await session.rollback()

                unchanged = await session.scalar(
                    select(ToolExecutionAuditRecord).where(
                        ToolExecutionAuditRecord.id == event_id
                    )
                )
                assert unchanged is not None
                assert unchanged.outcome == "success"

        asyncio.run(reject_mutation())
    finally:
        cleanup_test_context(user_id)
