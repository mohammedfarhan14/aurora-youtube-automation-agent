import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.core.config import Settings
from app.database.engine import get_session_factory
from app.main import app
from app.models.evidence_item import EvidenceItem
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.research_run import ResearchRun
from app.models.research_source import ResearchSource
from app.models.user import User
from app.research.contracts import EvidenceExcerpt, ResearchDocument
from app.workers.research_worker import (
    ClaimedResearchRun,
    ResearchProviderCancellationError,
    _cancel_task_bounded,
    _record_research_failure,
    _research_with_heartbeat,
    calculate_retry_delay,
    claim_research_batch,
    create_worker_id,
    process_research_run,
    recover_expired_research_leases,
    renew_research_lease,
)

client = TestClient(app)


class StaticResearchProvider:
    def __init__(self, documents):
        self.documents = documents
        self.calls = 0

    async def research(self, topic: str):
        self.calls += 1
        return self.documents


class FailingResearchProvider:
    async def research(self, topic: str):
        raise RuntimeError("Synthetic provider failure secret=provider-token-DO-NOT-LOG")


def make_test_settings() -> Settings:
    return Settings(
        _env_file=None,
        research_worker_batch_size=5,
        research_worker_max_concurrency=3,
        research_worker_poll_interval_seconds=1,
        research_worker_heartbeat_interval_seconds=1,
        research_worker_max_attempts=3,
        research_worker_initial_retry_seconds=2,
        research_worker_max_retry_seconds=10,
        research_worker_lease_seconds=30,
        research_worker_provider_timeout_seconds=5,
    )


def create_test_context():
    """Create a verified user, organization, and queued research run."""

    email = f"research-worker-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Research Worker Test",
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

    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    organization_response = client.post(
        "/api/v1/organizations",
        headers=headers,
        json={"name": f"Research Worker {uuid4().hex[:8]}"},
    )
    assert organization_response.status_code == 201

    organization_id = organization_response.json()["id"]

    run_response = client.post(
        f"/api/v1/organizations/{organization_id}/research-runs",
        headers=headers,
        json={"topic": "Synthetic renewable energy research"},
    )
    assert run_response.status_code == 201

    return user_id, run_response.json()["id"]


def cleanup_test_context(user_id: str):
    """Delete only the test user's research data and account."""

    async def cleanup():
        async with get_session_factory()() as session:
            async with session.begin():
                organization_ids = select(Organization.id).where(
                    Organization.created_by_user_id == user_id
                )

                run_ids = select(ResearchRun.id).where(
                    ResearchRun.organization_id.in_(organization_ids)
                )

                source_ids = select(ResearchSource.id).where(
                    ResearchSource.research_run_id.in_(run_ids)
                )

                await session.execute(
                    delete(EvidenceItem).where(EvidenceItem.research_source_id.in_(source_ids))
                )

                await session.execute(
                    delete(ResearchSource).where(ResearchSource.research_run_id.in_(run_ids))
                )

                await session.execute(
                    delete(ResearchRun).where(ResearchRun.organization_id.in_(organization_ids))
                )

                await session.execute(
                    delete(OrganizationMember).where(OrganizationMember.user_id == user_id)
                )

                await session.execute(
                    delete(Organization).where(Organization.created_by_user_id == user_id)
                )

                await session.execute(delete(User).where(User.id == user_id))

    asyncio.run(cleanup())


def make_documents():
    return (
        ResearchDocument(
            canonical_url="https://example.com/research/energy",
            title="Synthetic Energy Research",
            publisher="Example Research",
            source_type="webpage",
            published_at=None,
            retrieved_at=datetime(2026, 11, 10, tzinfo=UTC),
            content="Synthetic research content about renewable energy.",
            evidence=(
                EvidenceExcerpt(
                    excerpt="Synthetic evidence about renewable energy.",
                    locator="section-1",
                ),
            ),
        ),
    )


def test_calculate_retry_delay():
    settings = make_test_settings()

    assert calculate_retry_delay(1, settings=settings) == 2
    assert calculate_retry_delay(2, settings=settings) == 4
    assert calculate_retry_delay(3, settings=settings) == 8
    assert calculate_retry_delay(4, settings=settings) == 10
    assert calculate_retry_delay(5, settings=settings) == 10

    with pytest.raises(ValueError):
        calculate_retry_delay(0, settings=settings)


def test_claim_research_batch_claims_queued_run():
    user_id, run_id = create_test_context()
    settings = make_test_settings()

    try:
        claimed = asyncio.run(
            claim_research_batch(
                create_worker_id(),
                settings=settings,
            )
        )

        assert len(claimed) == 1
        assert claimed[0].id == UUID(run_id)
        assert claimed[0].attempt_count == 1
        assert claimed[0].lease_token is not None

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)

                assert run is not None
                assert run.status == "researching"
                assert run.attempt_count == 1
                assert run.lease_token == claimed[0].lease_token
                assert run.lease_expires_at is not None
                assert run.started_at is not None

        asyncio.run(verify())

    finally:
        cleanup_test_context(user_id)


def test_concurrent_workers_claim_distinct_runs():
    first_user_id, first_run_id = create_test_context()
    second_user_id, second_run_id = create_test_context()

    settings = make_test_settings()
    settings.research_worker_batch_size = 1
    settings.research_worker_max_concurrency = 1

    async def claim_concurrently():
        return await asyncio.gather(
            claim_research_batch(
                create_worker_id(),
                settings=settings,
            ),
            claim_research_batch(
                create_worker_id(),
                settings=settings,
            ),
        )

    try:
        first_claim, second_claim = asyncio.run(claim_concurrently())

        first_ids = {run.id for run in first_claim}
        second_ids = {run.id for run in second_claim}

        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == {
            UUID(first_run_id),
            UUID(second_run_id),
        }

        assert all(run.attempt_count == 1 for run in first_claim)
        assert all(run.attempt_count == 1 for run in second_claim)

    finally:
        cleanup_test_context(first_user_id)
        cleanup_test_context(second_user_id)


def test_renew_research_lease_and_reject_stale_token():
    user_id, run_id = create_test_context()
    settings = make_test_settings()

    try:
        claimed = asyncio.run(
            claim_research_batch(
                create_worker_id(),
                settings=settings,
            )
        )

        assert len(claimed) == 1
        lease = claimed[0]

        renewed = asyncio.run(
            renew_research_lease(
                run_id,
                lease.lease_token,
                settings=settings,
            )
        )
        assert renewed is True

        wrong_token_result = asyncio.run(
            renew_research_lease(
                run_id,
                uuid4(),
                settings=settings,
            )
        )
        assert wrong_token_result is False

        async def expire_lease():
            async with get_session_factory()() as session:
                async with session.begin():
                    run = await session.get(ResearchRun, run_id)
                    assert run is not None

                    run.lease_expires_at = await session.scalar(select(func.clock_timestamp()))

        asyncio.run(expire_lease())

        expired_result = asyncio.run(
            renew_research_lease(
                run_id,
                lease.lease_token,
                settings=settings,
            )
        )
        assert expired_result is False

    finally:
        cleanup_test_context(user_id)


def test_recover_expired_lease_requeues_run():
    user_id, run_id = create_test_context()
    settings = make_test_settings()

    try:
        claimed = asyncio.run(
            claim_research_batch(
                create_worker_id(),
                settings=settings,
            )
        )
        assert len(claimed) == 1

        async def expire_lease():
            async with get_session_factory()() as session:
                async with session.begin():
                    run = await session.get(ResearchRun, run_id)
                    assert run is not None

                    run.lease_expires_at = await session.scalar(select(func.clock_timestamp()))

        asyncio.run(expire_lease())

        recovered = asyncio.run(recover_expired_research_leases(settings=settings))

        assert recovered == 1

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)

                assert run is not None
                assert run.status == "queued"
                assert run.lease_token is None
                assert run.lease_expires_at is None
                assert run.failure_code == "lease_expired"
                assert run.completed_at is None

        asyncio.run(verify())

    finally:
        cleanup_test_context(user_id)


def test_recover_expired_lease_marks_max_attempts_failed():
    user_id, run_id = create_test_context()
    settings = make_test_settings()

    try:
        claimed = asyncio.run(
            claim_research_batch(
                create_worker_id(),
                settings=settings,
            )
        )
        assert len(claimed) == 1

        async def prepare_expired_run():
            async with get_session_factory()() as session:
                async with session.begin():
                    run = await session.get(ResearchRun, run_id)
                    assert run is not None

                    run.attempt_count = settings.research_worker_max_attempts
                    run.lease_expires_at = await session.scalar(select(func.clock_timestamp()))

        asyncio.run(prepare_expired_run())

        recovered = asyncio.run(recover_expired_research_leases(settings=settings))

        assert recovered == 1

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)

                assert run is not None
                assert run.status == "failed"
                assert run.failure_code == "lease_expired"
                assert run.completed_at is not None
                assert run.lease_token is None
                assert run.lease_expires_at is None

        asyncio.run(verify())

    finally:
        cleanup_test_context(user_id)


def test_process_research_run_persists_results():
    user_id, run_id = create_test_context()
    settings = make_test_settings()
    provider = StaticResearchProvider(make_documents())

    try:
        claimed = asyncio.run(
            claim_research_batch(
                create_worker_id(),
                settings=settings,
            )
        )
        assert len(claimed) == 1

        completed = asyncio.run(
            process_research_run(
                claimed[0],
                provider,
                settings=settings,
            )
        )

        assert completed is True
        assert provider.calls == 1

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)

                assert run is not None
                assert run.status == "completed"
                assert run.completed_at is not None
                assert run.lease_token is None
                assert run.lease_expires_at is None

                source_count = await session.scalar(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.research_run_id == run_id)
                )

                evidence_count = await session.scalar(
                    select(func.count())
                    .select_from(EvidenceItem)
                    .join(
                        ResearchSource,
                        EvidenceItem.research_source_id == ResearchSource.id,
                    )
                    .where(ResearchSource.research_run_id == run_id)
                )

                assert source_count == 1
                assert evidence_count == 1

        asyncio.run(verify())

    finally:
        cleanup_test_context(user_id)


def test_provider_failure_requeues_run_for_retry(caplog):
    user_id, run_id = create_test_context()
    settings = make_test_settings()

    try:
        claimed = asyncio.run(
            claim_research_batch(
                create_worker_id(),
                settings=settings,
            )
        )
        assert len(claimed) == 1

        completed = asyncio.run(
            process_research_run(
                claimed[0],
                FailingResearchProvider(),
                settings=settings,
            )
        )

        assert completed is False

        # Provider exception messages must never appear in worker logs.
        assert "provider-token-DO-NOT-LOG" not in caplog.text
        assert "SkillExecutionError" in caplog.text
        assert f"run_id={run_id}" in caplog.text

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)

                assert run is not None
                assert run.status == "queued"
                assert run.failure_code == "research_skillexecutionerror"
                assert run.failure_message is not None
                assert "Synthetic provider failure" not in run.failure_message
                assert run.lease_token is None
                assert run.lease_expires_at is None

        asyncio.run(verify())

    finally:
        cleanup_test_context(user_id)


def test_stale_worker_cannot_record_failure():
    user_id, run_id = create_test_context()
    settings = make_test_settings()

    try:
        claimed = asyncio.run(
            claim_research_batch(
                create_worker_id(),
                settings=settings,
            )
        )
        assert len(claimed) == 1

        lease = claimed[0]

        async def replace_lease_token():
            async with get_session_factory()() as session:
                async with session.begin():
                    run = await session.get(ResearchRun, run_id)
                    assert run is not None

                    run.lease_token = uuid4()

        asyncio.run(replace_lease_token())

        stale_claim = ClaimedResearchRun(
            id=lease.id,
            topic=lease.topic,
            attempt_count=lease.attempt_count,
            lease_token=lease.lease_token,
        )

        recorded = asyncio.run(
            _record_research_failure(
                stale_claim,
                RuntimeError("Synthetic stale-worker failure"),
                settings=settings,
                session_factory=get_session_factory(),
            )
        )

        assert recorded is False

        async def verify():
            async with get_session_factory()() as session:
                run = await session.get(ResearchRun, run_id)

                assert run is not None
                assert run.status == "researching"
                assert run.lease_token != lease.lease_token
                assert run.failure_code is None

        asyncio.run(verify())

    finally:
        cleanup_test_context(user_id)


def test_cancellation_resistant_provider_is_fatal_instead_of_retried(monkeypatch):
    async def run_test():
        cancellation_received = asyncio.Event()
        release_provider = asyncio.Event()

        class CancellationResistantProvider:
            async def research(self, topic: str):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancellation_received.set()
                    await release_provider.wait()
                    raise

        async def cancel_quickly(task, *, task_name, timeout_seconds=5.0):
            return await _cancel_task_bounded(
                task,
                task_name=task_name,
                timeout_seconds=0.05,
            )

        monkeypatch.setattr(
            "app.workers.research_worker._cancel_task_bounded",
            cancel_quickly,
        )

        settings = make_test_settings()
        settings.research_worker_provider_timeout_seconds = 0.01
        settings.research_worker_heartbeat_interval_seconds = 60

        run = ClaimedResearchRun(
            id=uuid4(),
            topic="Synthetic timeout test",
            attempt_count=1,
            lease_token=uuid4(),
        )

        try:
            with pytest.raises(ResearchProviderCancellationError):
                await _research_with_heartbeat(
                    run,
                    CancellationResistantProvider(),
                    settings=settings,
                    session_factory=None,
                )
            assert cancellation_received.is_set()
        finally:
            release_provider.set()
            pending_provider_tasks = [
                task
                for task in asyncio.all_tasks()
                if task.get_name() == f"research-provider-{run.id}"
            ]
            if pending_provider_tasks:
                await asyncio.gather(*pending_provider_tasks, return_exceptions=True)

    asyncio.run(run_test())

def test_shutdown_cancellation_of_provider_that_ignores_cancellation_is_fatal(monkeypatch):
    async def run_test():
        provider_started = asyncio.Event()
        cancellation_received = asyncio.Event()
        release_provider = asyncio.Event()

        class CancellationResistantProvider:
            async def research(self, topic: str):
                provider_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancellation_received.set()
                    await release_provider.wait()
                    raise

        original_cancel = _cancel_task_bounded

        async def cancel_quickly(task, *, task_name, timeout_seconds=5.0):
            return await original_cancel(
                task,
                task_name=task_name,
                timeout_seconds=0.05,
            )

        monkeypatch.setattr(
            "app.workers.research_worker._cancel_task_bounded",
            cancel_quickly,
        )

        settings = make_test_settings()
        settings.research_worker_provider_timeout_seconds = 30
        settings.research_worker_heartbeat_interval_seconds = 60

        run = ClaimedResearchRun(
            id=uuid4(),
            topic="Synthetic shutdown cancellation test",
            attempt_count=1,
            lease_token=uuid4(),
        )

        research_task = asyncio.create_task(
            _research_with_heartbeat(
                run,
                CancellationResistantProvider(),
                settings=settings,
                session_factory=None,
            )
        )

        await provider_started.wait()
        research_task.cancel()

        try:
            with pytest.raises(
                ResearchProviderCancellationError,
                match="ignored cancellation during worker shutdown",
            ):
                await research_task
            assert cancellation_received.is_set()
        finally:
            release_provider.set()
            pending_provider_tasks = [
                task
                for task in asyncio.all_tasks()
                if task.get_name() == f"research-provider-{run.id}"
            ]
            if pending_provider_tasks:
                await asyncio.gather(*pending_provider_tasks, return_exceptions=True)

    asyncio.run(run_test())
