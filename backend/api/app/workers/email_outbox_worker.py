import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select, update

from app.core.config import Settings, get_settings
from app.database.engine import get_session_factory
from app.models.email_outbox import EmailOutbox
from app.services.email import EmailDeliveryError, EmailMessage, EmailSender
from app.services.email_outbox_crypto import (
    decrypt_payload,
    get_encryption_keys,
)


class EmailLeaseLostError(RuntimeError):
    """Raised when a worker can no longer prove ownership of an email lease."""


def _owns_active_lease(
    message: EmailOutbox,
    worker_id: str,
    now: datetime,
    settings: Settings,
) -> bool:
    """Check that a worker still owns a non-expired processing lease."""

    if (
        message.status != "processing"
        or message.locked_by != worker_id
        or message.locked_at is None
    ):
        return False

    lease_expiry = message.locked_at + timedelta(seconds=settings.email_outbox_lease_seconds)

    return now < lease_expiry


async def claim_email_batch(
    worker_id: str,
    *,
    settings: Settings | None = None,
) -> list[EmailOutbox]:
    """Atomically claim a bounded batch of available email messages."""

    if not worker_id.strip():
        raise ValueError("Worker ID cannot be empty.")

    settings = settings or get_settings()
    session_factory = get_session_factory()
    claim_limit = min(
        settings.email_outbox_batch_size,
        settings.email_outbox_max_concurrency,
    )

    now = datetime.now(UTC)

    async with session_factory() as session:
        async with session.begin():
            statement = (
                select(EmailOutbox)
                .where(
                    EmailOutbox.status == "pending",
                    EmailOutbox.available_at <= now,
                )
                .order_by(
                    EmailOutbox.available_at,
                    EmailOutbox.created_at,
                    EmailOutbox.id,
                )
                .limit(claim_limit)
                .with_for_update(skip_locked=True)
            )

            result = await session.scalars(statement)
            messages = list(result.all())

            for message in messages:
                message.status = "processing"
                message.locked_at = now
                message.locked_by = worker_id
                message.attempts += 1

        return messages


async def renew_email_lease(
    message_id: UUID,
    worker_id: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """Extend a lease only while this worker still owns an active lease."""

    settings = settings or get_settings()
    session_factory = get_session_factory()
    now = datetime.now(UTC)
    lease_expiry = now - timedelta(seconds=settings.email_outbox_lease_seconds)

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(EmailOutbox)
                .where(
                    EmailOutbox.id == message_id,
                    EmailOutbox.status == "processing",
                    EmailOutbox.locked_by == worker_id,
                    EmailOutbox.locked_at > lease_expiry,
                )
                .values(locked_at=now)
            )

            return result.rowcount == 1


async def _maintain_email_lease(
    message_id: UUID,
    worker_id: str,
    stop_event: asyncio.Event,
    *,
    settings: Settings,
) -> None:
    """Renew a processing lease periodically until delivery finishes."""

    interval = max(
        0.05,
        settings.email_outbox_lease_seconds / 3,
    )

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=interval,
            )
        except TimeoutError:
            renewed = await renew_email_lease(
                message_id,
                worker_id,
                settings=settings,
            )

            if not renewed:
                raise EmailLeaseLostError("Email outbox lease was lost during delivery.")


async def _send_with_lease_heartbeat(
    message_id: UUID,
    worker_id: str,
    sender: EmailSender,
    message: EmailMessage,
    *,
    settings: Settings,
) -> None:
    """Deliver an email while renewing its lease in a background task."""

    # A claimed message may have spent most of its lease waiting for the
    # worker to start processing it. Renew before the external side effect so
    # delivery never begins on a lease that is about to expire.
    renewed = await renew_email_lease(
        message_id,
        worker_id,
        settings=settings,
    )
    if not renewed:
        raise EmailLeaseLostError("Email outbox lease was lost before delivery.")

    stop_event = asyncio.Event()
    delivery_task = asyncio.create_task(
        asyncio.wait_for(
            sender.send_email(message),
            timeout=settings.email_outbox_delivery_timeout_seconds,
        )
    )
    heartbeat_task = asyncio.create_task(
        _maintain_email_lease(
            message_id,
            worker_id,
            stop_event,
            settings=settings,
        )
    )

    try:
        done, _ = await asyncio.wait(
            {delivery_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if heartbeat_task in done:
            delivery_task.cancel()
            await asyncio.gather(
                delivery_task,
                return_exceptions=True,
            )
            await heartbeat_task
            raise EmailLeaseLostError("Email lease heartbeat stopped unexpectedly.")

        await delivery_task
    except asyncio.CancelledError:
        delivery_task.cancel()
        await asyncio.gather(
            delivery_task,
            return_exceptions=True,
        )
        raise
    finally:
        stop_event.set()

        if not heartbeat_task.done():
            await heartbeat_task


async def recover_expired_email_leases(
    *,
    settings: Settings | None = None,
) -> int:
    """Recover messages abandoned by workers whose leases expired."""

    settings = settings or get_settings()
    session_factory = get_session_factory()

    now = datetime.now(UTC)
    lease_expiry = now - timedelta(seconds=settings.email_outbox_lease_seconds)

    recovered_count = 0

    async with session_factory() as session:
        async with session.begin():
            statement = (
                select(EmailOutbox)
                .where(
                    EmailOutbox.status == "processing",
                    EmailOutbox.locked_at <= lease_expiry,
                )
                .order_by(EmailOutbox.locked_at, EmailOutbox.id)
                .limit(settings.email_outbox_batch_size)
                .with_for_update(skip_locked=True)
            )

            result = await session.scalars(statement)
            messages = list(result.all())

            for message in messages:
                message.locked_at = None
                message.locked_by = None

                if message.attempts >= settings.email_outbox_max_attempts:
                    message.status = "failed"
                    message.last_error = "Processing lease expired after maximum attempts."
                else:
                    message.status = "pending"
                    message.available_at = now + timedelta(
                        seconds=calculate_retry_delay(
                            message.attempts,
                            settings=settings,
                        )
                    )
                    message.last_error = "Processing lease expired; message returned to queue."

                recovered_count += 1

    return recovered_count


def calculate_retry_delay(
    attempts: int,
    *,
    settings: Settings | None = None,
) -> int:
    """Calculate capped exponential retry delay in seconds."""

    if attempts < 1:
        raise ValueError("Attempt count must be at least 1.")

    settings = settings or get_settings()

    delay = settings.email_outbox_initial_retry_seconds * (2 ** (attempts - 1))

    return min(delay, 3600)


async def process_email_message(
    message_id: UUID,
    worker_id: str,
    sender: EmailSender,
    *,
    settings: Settings | None = None,
) -> bool:
    """Deliver one claimed message and persist its outcome."""

    settings = settings or get_settings()
    session_factory = get_session_factory()

    # Read the message and verify that this worker owns its lease.
    async with session_factory() as session:
        message = await session.get(EmailOutbox, message_id)

        if message is None or not _owns_active_lease(
            message,
            worker_id,
            datetime.now(UTC),
            settings,
        ):
            return False

        recipient = message.recipient
        subject = message.subject
        encrypted_payload = message.encrypted_payload
        key_version = message.key_version
        attempts = message.attempts

    try:
        body = decrypt_payload(
            encrypted_payload,
            get_encryption_keys(settings),
            expected_key_version=key_version,
        )

        await _send_with_lease_heartbeat(
            message_id,
            worker_id,
            sender,
            EmailMessage(
                to_email=recipient,
                subject=subject,
                text_body=body,
                idempotency_key=f"email-outbox:{message_id}",
            ),
            settings=settings,
        )

    except asyncio.CancelledError:
        raise
    except EmailLeaseLostError:
        return False
    except Exception as exc:
        # Store a generic error rather than provider details that could
        # contain credentials, message contents, or personal information.
        error_message = f"Email delivery failed ({type(exc).__name__})."

        async with session_factory() as session:
            async with session.begin():
                message = await session.get(
                    EmailOutbox,
                    message_id,
                    with_for_update=True,
                )

                if message is None or not _owns_active_lease(
                    message,
                    worker_id,
                    datetime.now(UTC),
                    settings,
                ):
                    return False

                message.locked_at = None
                message.locked_by = None
                message.last_error = (
                    f"Email delivery failed ({exc.error_code})."
                    if isinstance(exc, EmailDeliveryError)
                    else error_message
                )

                if (
                    isinstance(exc, EmailDeliveryError)
                    and not exc.retryable
                ) or attempts >= settings.email_outbox_max_attempts:
                    message.status = "failed"
                else:
                    message.status = "pending"
                    message.available_at = datetime.now(UTC) + timedelta(
                        seconds=calculate_retry_delay(
                            attempts,
                            settings=settings,
                        )
                    )

        return False

    async with session_factory() as session:
        async with session.begin():
            message = await session.get(
                EmailOutbox,
                message_id,
                with_for_update=True,
            )

            if message is None or not _owns_active_lease(
                message,
                worker_id,
                datetime.now(UTC),
                settings,
            ):
                return False

            message.status = "sent"
            message.sent_at = datetime.now(UTC)
            message.locked_at = None
            message.locked_by = None
            message.last_error = None

    return True


async def get_email_outbox_health(
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, int | None]:
    """Return operational queue metrics using the active-status partial indexes."""

    del settings  # Reserved for parity with worker operations and future filtering.
    session_factory = get_session_factory()
    observed_at = now or datetime.now(UTC)
    pending = EmailOutbox.status == "pending"
    processing = EmailOutbox.status == "processing"
    available_pending = and_(pending, EmailOutbox.available_at <= observed_at)

    # Keep status predicates in each subquery's WHERE clause. This lets PostgreSQL
    # use the partial pending/processing indexes instead of scanning historical rows
    # and applying FILTER aggregates to the entire outbox table.
    statement = select(
        select(func.count())
        .select_from(EmailOutbox)
        .where(pending)
        .scalar_subquery()
        .label("pending_count"),
        select(func.count())
        .select_from(EmailOutbox)
        .where(processing)
        .scalar_subquery()
        .label("processing_count"),
        select(func.count())
        .select_from(EmailOutbox)
        .where(available_pending)
        .scalar_subquery()
        .label("available_pending_count"),
        select(func.min(EmailOutbox.created_at))
        .where(available_pending)
        .scalar_subquery()
        .label("oldest_available_pending_at"),
        select(func.min(EmailOutbox.locked_at))
        .where(processing)
        .scalar_subquery()
        .label("oldest_processing_at"),
    )

    async with session_factory() as session:
        row = (await session.execute(statement)).one()

    def age_seconds(timestamp: datetime | None) -> int | None:
        if timestamp is None:
            return None
        return max(0, int((observed_at - timestamp).total_seconds()))

    return {
        "pending_count": int(row.pending_count or 0),
        "processing_count": int(row.processing_count or 0),
        "available_pending_count": int(row.available_pending_count or 0),
        "oldest_available_pending_age_seconds": age_seconds(
            row.oldest_available_pending_at
        ),
        "oldest_processing_age_seconds": age_seconds(row.oldest_processing_at),
    }

def create_worker_id() -> str:
    """Generate a unique identifier for this worker process."""

    return f"email-worker-{uuid4()}"
