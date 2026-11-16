import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

import app.workers.email_outbox_worker as worker_module
from app.core.config import Settings
from app.database.engine import get_session_factory
from app.models.email_outbox import EmailOutbox
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.services.auth import register_user
from app.services.email import EmailDeliveryError, EmailMessage
from app.services.email_outbox import enqueue_email_message
from app.workers.email_outbox_worker import (
    claim_email_batch,
    create_worker_id,
    get_email_outbox_health,
    process_email_message,
    recover_expired_email_leases,
    renew_email_lease,
)


class FakeEmailSender:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.messages: list[EmailMessage] = []
        self.should_fail = should_fail

    async def send_email(self, message: EmailMessage) -> None:
        if self.should_fail:
            raise RuntimeError("Simulated email provider failure.")

        self.messages.append(message)


TEST_KEY = "a" * 64


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        _env_file=None,
        email_outbox_encryption_key=TEST_KEY,
        email_outbox_batch_size=1,
    )


async def create_test_outbox_message(settings: Settings):
    email = f"worker-{uuid4().hex}@example.com"

    async with get_session_factory()() as session:
        user = await register_user(
            session,
            RegisterRequest(
                email=email,
                display_name="Worker Test",
                password="Aurora-Secure-Password-2026",
            ),
        )
        user_id = user.id

    message = EmailMessage(
        to_email=email,
        subject="Worker integration test",
        text_body="Test verification message.",
    )

    async with get_session_factory()() as session:
        async with session.begin():
            outbox_message = await enqueue_email_message(
                session,
                message,
                message_type="test",
                user_id=user_id,
                settings=settings,
            )
            outbox_id = outbox_message.id

    return user_id, outbox_id


async def cleanup_test_data(user_id) -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(delete(EmailOutbox).where(EmailOutbox.user_id == user_id))
            await session.execute(delete(User).where(User.id == user_id))


@pytest.mark.anyio
async def test_claim_email_batch_claims_pending_message(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()

    async with get_session_factory()() as session:
        async with session.begin():
            message = await session.get(EmailOutbox, outbox_id)
            assert message is not None
            message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

    try:
        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )

        assert len(claimed) == 1
        assert claimed[0].id == outbox_id

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)

            assert persisted is not None
            assert persisted.status == "processing"
            assert persisted.locked_by == worker_id
            assert persisted.locked_at is not None
            assert persisted.attempts == 1

    finally:
        await cleanup_test_data(user_id)



@pytest.mark.anyio
async def test_concurrent_workers_claim_distinct_messages(test_settings) -> None:
    first_user_id, first_outbox_id = await create_test_outbox_message(test_settings)
    second_user_id, second_outbox_id = await create_test_outbox_message(test_settings)
    worker_a = create_worker_id()
    worker_b = create_worker_id()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                messages = await session.scalars(
                    select(EmailOutbox).where(
                        EmailOutbox.id.in_([first_outbox_id, second_outbox_id])
                    )
                )
                for message in messages:
                    message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

        claimed_a, claimed_b = await asyncio.gather(
            claim_email_batch(worker_a, settings=test_settings),
            claim_email_batch(worker_b, settings=test_settings),
        )
        ids_a = {message.id for message in claimed_a}
        ids_b = {message.id for message in claimed_b}

        assert ids_a.isdisjoint(ids_b)
        assert ids_a | ids_b == {first_outbox_id, second_outbox_id}

        async with get_session_factory()() as session:
            persisted = await session.scalars(
                select(EmailOutbox).where(
                    EmailOutbox.id.in_([first_outbox_id, second_outbox_id])
                )
            )
            by_id = {message.id: message for message in persisted}

        assert all(by_id[message_id].status == "processing" for message_id in ids_a | ids_b)
        assert all(
            by_id[message_id].locked_by in {worker_a, worker_b}
            for message_id in ids_a | ids_b
        )
        assert all(by_id[message_id].attempts == 1 for message_id in ids_a | ids_b)
    finally:
        await cleanup_test_data(first_user_id)
        await cleanup_test_data(second_user_id)


@pytest.mark.anyio
async def test_email_outbox_health_reports_counts_and_ages(test_settings) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    observed_at = datetime.now(UTC)

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.created_at = observed_at - timedelta(seconds=90)
                message.available_at = observed_at - timedelta(seconds=30)

        health = await get_email_outbox_health(settings=test_settings, now=observed_at)

        assert health["pending_count"] >= 1
        assert health["available_pending_count"] >= 1
        assert health["oldest_available_pending_age_seconds"] >= 90
        assert health["processing_count"] >= 0
        assert "sent_count" not in health
        assert "failed_count" not in health
    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_process_email_message_sends_and_marks_sent(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()

    async with get_session_factory()() as session:
        async with session.begin():
            message = await session.get(EmailOutbox, outbox_id)
            assert message is not None
            message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

    sender = FakeEmailSender()

    try:
        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )

        assert any(message.id == outbox_id for message in claimed)

        delivered = await process_email_message(
            outbox_id,
            worker_id,
            sender,
            settings=test_settings,
        )

        assert delivered is True
        assert len(sender.messages) == 1
        assert sender.messages[0].text_body == ("Test verification message.")
        assert sender.messages[0].idempotency_key == (f"email-outbox:{outbox_id}")

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)

            assert persisted is not None
            assert persisted.status == "sent"
            assert persisted.sent_at is not None
            assert persisted.locked_at is None
            assert persisted.locked_by is None
            assert persisted.last_error is None

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_process_email_message_renews_lease_during_delivery(
    test_settings,
    monkeypatch,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()
    test_settings.email_outbox_lease_seconds = 2
    test_settings.email_outbox_delivery_timeout_seconds = 1.5

    real_renew = worker_module.renew_email_lease
    renew_spy = AsyncMock(wraps=real_renew)
    monkeypatch.setattr(worker_module, "renew_email_lease", renew_spy)

    class SlowEmailSender(FakeEmailSender):
        async def send_email(self, message: EmailMessage) -> None:
            await asyncio.sleep(1.2)
            await super().send_email(message)

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )
        assert any(message.id == outbox_id for message in claimed)

        sender = SlowEmailSender()
        delivered = await process_email_message(
            outbox_id,
            worker_id,
            sender,
            settings=test_settings,
        )

        assert delivered is True
        assert renew_spy.await_count >= 1
        assert sender.messages[0].idempotency_key == (f"email-outbox:{outbox_id}")

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "sent"

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_renew_email_lease_rejects_a_different_worker(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.status = "processing"
                message.locked_by = worker_id
                message.locked_at = datetime.now(UTC)

        renewed = await renew_email_lease(
            outbox_id,
            create_worker_id(),
            settings=test_settings,
        )

        assert renewed is False

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_process_email_message_retries_after_provider_failure(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()
    sender = FakeEmailSender(should_fail=True)

    async with get_session_factory()() as session:
        async with session.begin():
            message = await session.get(EmailOutbox, outbox_id)
            assert message is not None
            message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

    try:
        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )
        assert any(message.id == outbox_id for message in claimed)

        delivered = await process_email_message(
            outbox_id,
            worker_id,
            sender,
            settings=test_settings,
        )

        assert delivered is False

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)

            assert persisted is not None
            assert persisted.status == "pending"
            assert persisted.attempts == 1
            assert persisted.available_at > datetime.now(UTC)
            assert persisted.locked_at is None
            assert persisted.locked_by is None
            assert persisted.last_error == ("Email delivery failed (RuntimeError).")

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_process_email_message_rejects_expired_lease(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()

    async with get_session_factory()() as session:
        async with session.begin():
            message = await session.get(EmailOutbox, outbox_id)
            assert message is not None
            message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

    sender = FakeEmailSender()

    try:
        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )
        assert any(message.id == outbox_id for message in claimed)

        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.locked_at = datetime(2000, 1, 1, tzinfo=UTC)

        delivered = await process_email_message(
            outbox_id,
            worker_id,
            sender,
            settings=test_settings,
        )

        assert delivered is False
        assert sender.messages == []

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "processing"
            assert persisted.locked_by == worker_id

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_recover_expired_email_lease_applies_retry_backoff(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.status = "processing"
                message.locked_by = worker_id
                message.locked_at = datetime(2000, 1, 1, tzinfo=UTC)
                message.attempts = 2

        recovered = await recover_expired_email_leases(
            settings=test_settings,
        )
        assert recovered >= 1

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "pending"
            assert persisted.locked_at is None
            assert persisted.locked_by is None
            assert persisted.attempts == 2

            expected_delay = 60
            now = datetime.now(UTC)
            assert persisted.available_at >= now + timedelta(seconds=expected_delay - 2)
            assert persisted.available_at <= now + timedelta(seconds=expected_delay + 2)
            assert persisted.last_error == ("Processing lease expired; message returned to queue.")

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_process_email_message_propagates_cancellation(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()

    class CancelledEmailSender:
        async def send_email(self, message: EmailMessage) -> None:
            raise asyncio.CancelledError()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )
        assert any(message.id == outbox_id for message in claimed)

        with pytest.raises(asyncio.CancelledError):
            await process_email_message(
                outbox_id,
                worker_id,
                CancelledEmailSender(),
                settings=test_settings,
            )

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "processing"
            assert persisted.locked_by == worker_id
            assert persisted.attempts == 1

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_process_email_message_retries_after_provider_timeout(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()
    test_settings.email_outbox_delivery_timeout_seconds = 0.05

    class SlowEmailSender:
        async def send_email(self, message: EmailMessage) -> None:
            await asyncio.sleep(1)

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )
        assert any(message.id == outbox_id for message in claimed)

        delivered = await process_email_message(
            outbox_id,
            worker_id,
            SlowEmailSender(),
            settings=test_settings,
        )
        assert delivered is False

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "pending"
            assert persisted.attempts == 1
            assert persisted.locked_at is None
            assert persisted.locked_by is None
            assert persisted.last_error == ("Email delivery failed (TimeoutError).")
            assert persisted.available_at > datetime.now(UTC)

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_process_email_message_marks_permanent_provider_failure_failed(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()

    class PermanentFailureSender:
        async def send_email(self, message: EmailMessage) -> None:
            raise EmailDeliveryError(
                "Email provider rejected the delivery request.",
                retryable=False,
                error_code="provider_http_401",
            )

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )
        assert any(message.id == outbox_id for message in claimed)

        delivered = await process_email_message(
            outbox_id,
            worker_id,
            PermanentFailureSender(),
            settings=test_settings,
        )

        assert delivered is False

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "failed"
            assert persisted.attempts == 1
            assert persisted.locked_at is None
            assert persisted.locked_by is None
            assert persisted.last_error == "Email delivery failed (provider_http_401)."

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_process_email_message_retries_transient_provider_failure(
    test_settings,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()

    class TransientFailureSender:
        async def send_email(self, message: EmailMessage) -> None:
            raise EmailDeliveryError(
                "Email provider could not be reached.",
                retryable=True,
                error_code="provider_network_error",
            )

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )
        assert any(message.id == outbox_id for message in claimed)

        delivered = await process_email_message(
            outbox_id,
            worker_id,
            TransientFailureSender(),
            settings=test_settings,
        )

        assert delivered is False

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "pending"
            assert persisted.attempts == 1
            assert persisted.locked_at is None
            assert persisted.locked_by is None
            assert persisted.last_error == "Email delivery failed (provider_network_error)."
            assert persisted.available_at > datetime.now(UTC)

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_process_email_message_renews_nearly_expired_lease_before_delivery(
    test_settings,
    monkeypatch,
) -> None:
    user_id, outbox_id = await create_test_outbox_message(test_settings)
    worker_id = create_worker_id()
    test_settings.email_outbox_lease_seconds = 2
    test_settings.email_outbox_delivery_timeout_seconds = 1.5

    real_renew = worker_module.renew_email_lease
    renew_spy = AsyncMock(wraps=real_renew)
    monkeypatch.setattr(worker_module, "renew_email_lease", renew_spy)

    class SlowEmailSender(FakeEmailSender):
        async def send_email(self, message: EmailMessage) -> None:
            await asyncio.sleep(0.35)
            await super().send_email(message)

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.available_at = datetime(2000, 1, 1, tzinfo=UTC)

        claimed = await claim_email_batch(
            worker_id,
            settings=test_settings,
        )
        assert any(message.id == outbox_id for message in claimed)

        # Leave less time on the lease than the simulated delivery duration.
        async with get_session_factory()() as session:
            async with session.begin():
                message = await session.get(EmailOutbox, outbox_id)
                assert message is not None
                message.locked_at = datetime.now(UTC) - timedelta(seconds=1.8)

        sender = SlowEmailSender()
        delivered = await process_email_message(
            outbox_id,
            worker_id,
            sender,
            settings=test_settings,
        )

        assert delivered is True
        assert renew_spy.await_count >= 1
        assert len(sender.messages) == 1

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "sent"
            assert persisted.locked_at is None
            assert persisted.locked_by is None

    finally:
        await cleanup_test_data(user_id)


@pytest.mark.anyio
async def test_email_outbox_health_counts_only_operational_queue_states(
    test_settings,
) -> None:
    observed_at = datetime.now(UTC)
    created = [
        await create_test_outbox_message(test_settings)
        for _ in range(5)
    ]
    user_ids = [user_id for user_id, _ in created]
    message_ids = [message_id for _, message_id in created]
    baseline = await get_email_outbox_health(now=observed_at)

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                messages = list(
                    await session.scalars(
                        select(EmailOutbox).where(EmailOutbox.id.in_(message_ids))
                    )
                )
                by_id = {message.id: message for message in messages}

                available_pending = by_id[message_ids[0]]
                available_pending.created_at = observed_at - timedelta(seconds=90)
                available_pending.available_at = observed_at - timedelta(seconds=30)

                delayed_pending = by_id[message_ids[1]]
                delayed_pending.created_at = observed_at - timedelta(seconds=120)
                delayed_pending.available_at = observed_at + timedelta(seconds=60)

                processing = by_id[message_ids[2]]
                processing.status = "processing"
                processing.locked_by = "health-test-worker"
                processing.locked_at = observed_at - timedelta(seconds=45)
                processing.attempts = 1

                sent = by_id[message_ids[3]]
                sent.status = "sent"
                sent.sent_at = observed_at

                failed = by_id[message_ids[4]]
                failed.status = "failed"
                failed.last_error = "sanitized test failure"

        health = await get_email_outbox_health(now=observed_at)

        # Health metrics describe the global queue, which may contain records
        # created by other tests. Assert this test's contribution as a delta.
        assert health["pending_count"] == baseline["pending_count"] - 3
        assert (
            health["available_pending_count"]
            == baseline["available_pending_count"] + 1
        )
        assert health["processing_count"] == baseline["processing_count"] + 1
        assert health["oldest_available_pending_age_seconds"] >= 90
        assert health["oldest_processing_age_seconds"] >= 45
        assert "sent_count" not in health
        assert "failed_count" not in health
    finally:
        for user_id in user_ids:
            await cleanup_test_data(user_id)



@pytest.mark.anyio
async def test_email_outbox_health_query_uses_partial_indexes_with_large_history() -> None:
    marker = f"health-benchmark-{uuid4().hex}-"
    queries = (
        (
            "pending_count",
            "SELECT count(*) FROM email_outbox WHERE status = 'pending'",
            "ix_email_outbox_pending_available",
        ),
        (
            "processing_count",
            "SELECT count(*) FROM email_outbox WHERE status = 'processing'",
            "ix_email_outbox_processing_locked",
        ),
        (
            "available_pending_count",
            "SELECT count(*) FROM email_outbox "
            "WHERE status = 'pending' AND available_at <= now()",
            "ix_email_outbox_pending_available",
        ),
        (
            "oldest_available_pending",
            "SELECT min(created_at) FROM email_outbox "
            "WHERE status = 'pending' AND available_at <= now()",
            "ix_email_outbox_pending_available",
        ),
        (
            "oldest_processing",
            "SELECT min(locked_at) FROM email_outbox WHERE status = 'processing'",
            "ix_email_outbox_processing_locked",
        ),
    )

    async def capture_plans() -> dict[str, str]:
        plans: dict[str, str] = {}
        async with get_session_factory()() as session:
            for name, query, _ in queries:
                result = await session.scalar(
                    text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}")
                )
                plans[name] = json.dumps(result)
        return plans

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO email_outbox (
                            id, recipient, subject, message_type, encrypted_payload,
                            key_version, status, attempts, available_at, created_at
                        )
                        SELECT
                            gen_random_uuid(),
                            :marker || series || '@example.com',
                            'Health benchmark',
                            'health_benchmark',
                            'benchmark-ciphertext',
                            1,
                            'pending',
                            0,
                            now() - interval '1 minute',
                            now() - interval '2 minutes'
                        FROM generate_series(1, 5) AS series
                        """
                    ),
                    {"marker": marker},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO email_outbox (
                            id, recipient, subject, message_type, encrypted_payload,
                            key_version, status, attempts, available_at, locked_at,
                            locked_by, created_at
                        )
                        SELECT
                            gen_random_uuid(),
                            :marker || 'processing-' || series || '@example.com',
                            'Health benchmark',
                            'health_benchmark',
                            'benchmark-ciphertext',
                            1,
                            'processing',
                            1,
                            now() - interval '1 minute',
                            now() - interval '30 seconds',
                            'health-benchmark-worker',
                            now() - interval '2 minutes'
                        FROM generate_series(1, 5) AS series
                        """
                    ),
                    {"marker": marker},
                )
                await session.execute(text("ANALYZE email_outbox"))

        small_dataset_plans = await capture_plans()
        assert set(small_dataset_plans) == {name for name, _, _ in queries}

        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO email_outbox (
                            id, recipient, subject, message_type, encrypted_payload,
                            key_version, status, attempts, available_at, created_at, sent_at
                        )
                        SELECT
                            gen_random_uuid(),
                            :marker || 'history-' || series || '@example.com',
                            'Historical benchmark',
                            'health_benchmark',
                            'benchmark-ciphertext',
                            1,
                            CASE WHEN series % 2 = 0 THEN 'sent' ELSE 'failed' END,
                            1,
                            now() - interval '7 days',
                            now() - interval '7 days',
                            CASE
                                WHEN series % 2 = 0 THEN now() - interval '7 days'
                                ELSE NULL
                            END
                        FROM generate_series(1, 50000) AS series
                        """
                    ),
                    {"marker": marker},
                )
                await session.execute(text("ANALYZE email_outbox"))

        large_dataset_plans = await capture_plans()
        for name, _, expected_index in queries:
            assert expected_index in large_dataset_plans[name], (
                f"{name} did not use its active-queue partial index: "
                f"{large_dataset_plans[name]}"
            )

        health = await get_email_outbox_health()
        assert health["pending_count"] >= 5
        assert health["processing_count"] >= 5
        assert health["available_pending_count"] >= 5
    finally:
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    text("DELETE FROM email_outbox WHERE recipient LIKE :pattern"),
                    {"pattern": f"{marker}%"},
                )
                await session.execute(text("ANALYZE email_outbox"))
