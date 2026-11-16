import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.database.engine import get_session_factory
from app.models.research_run import ResearchRun
from app.research.contracts import ResearchProvider
from app.research.service import (
    ResearchLeaseLostError,
    _persist_documents,
)
from app.services.operations_alerts import enqueue_operations_alert
from app.skills.execution import ToolAuthorizationError
from app.workers.research_skill_runtime import AuditedResearchProvider

logger = logging.getLogger(__name__)


class ResearchProviderCancellationError(RuntimeError):
    """Raised when a provider ignores cancellation and the worker must fail-stop."""


@dataclass(frozen=True, slots=True)
class ClaimedResearchRun:
    id: UUID
    topic: str
    attempt_count: int
    lease_token: UUID
    organization_id: UUID | None = None
    requested_by_user_id: UUID | None = None


def create_worker_id() -> str:
    """Generate a unique identifier for this worker process."""
    return f"research-worker-{uuid4()}"


def calculate_retry_delay(
    attempt_count: int,
    *,
    settings: Settings | None = None,
) -> int:
    """Calculate capped exponential retry delay in seconds."""

    if attempt_count < 1:
        raise ValueError("Attempt count must be at least 1.")

    settings = settings or get_settings()

    initial_delay = settings.research_worker_initial_retry_seconds
    maximum_delay = settings.research_worker_max_retry_seconds

    # Bound the exponent before calculating the power. This keeps the
    # calculation safe even if attempt_count becomes unexpectedly large.
    maximum_exponent = (maximum_delay // initial_delay).bit_length()
    exponent = min(attempt_count - 1, maximum_exponent)
    delay = initial_delay * (2**exponent)

    return min(delay, maximum_delay)


async def claim_research_batch(
    worker_id: str,
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[ClaimedResearchRun]:
    """Atomically claim a bounded batch of queued research runs."""

    if not worker_id.strip():
        raise ValueError("Worker ID cannot be empty.")

    settings = settings or get_settings()
    session_factory = session_factory or get_session_factory()

    claim_limit = min(
        settings.research_worker_batch_size,
        settings.research_worker_max_concurrency,
    )

    claimed: list[ClaimedResearchRun] = []

    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(select(func.clock_timestamp()))

            if now is None:
                raise RuntimeError("Could not obtain the database clock.")

            lease_expiry = now + timedelta(seconds=settings.research_worker_lease_seconds)

            statement = (
                select(ResearchRun)
                .where(
                    ResearchRun.status == "queued",
                    ResearchRun.next_attempt_at <= now,
                )
                .order_by(
                    ResearchRun.next_attempt_at,
                    ResearchRun.created_at,
                    ResearchRun.id,
                )
                .limit(claim_limit)
                .with_for_update(skip_locked=True)
            )

            result = await session.scalars(statement)
            runs = list(result.all())

            for run in runs:
                lease_token = uuid4()

                run.status = "researching"
                run.attempt_count += 1
                run.lease_token = lease_token
                run.lease_expires_at = lease_expiry
                run.started_at = run.started_at or now
                run.completed_at = None
                run.failure_code = None
                run.failure_message = None

                claimed.append(
                    ClaimedResearchRun(
                        id=run.id,
                        organization_id=run.organization_id,
                        requested_by_user_id=run.requested_by_user_id,
                        topic=run.topic,
                        attempt_count=run.attempt_count,
                        lease_token=lease_token,
                    )
                )

    if claimed:
        logger.info(
            "Research worker %s claimed %s run(s).",
            worker_id,
            len(claimed),
        )

    return claimed


async def renew_research_lease(
    research_run_id: UUID,
    lease_token: UUID,
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """Extend a lease only while its token is still valid."""

    settings = settings or get_settings()
    session_factory = session_factory or get_session_factory()

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(ResearchRun)
                .where(
                    ResearchRun.id == research_run_id,
                    ResearchRun.status == "researching",
                    ResearchRun.lease_token == lease_token,
                    ResearchRun.lease_expires_at > func.clock_timestamp(),
                )
                .values(
                    lease_expires_at=(
                        func.clock_timestamp()
                        + timedelta(seconds=settings.research_worker_lease_seconds)
                    )
                )
            )

            return result.rowcount == 1


async def recover_expired_research_leases(
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Return expired research leases to the queue or mark them failed."""

    settings = settings or get_settings()
    session_factory = session_factory or get_session_factory()

    recovered_count = 0

    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(select(func.clock_timestamp()))

            if now is None:
                raise RuntimeError("Could not obtain the database clock.")

            statement = (
                select(ResearchRun)
                .where(
                    ResearchRun.status == "researching",
                    ResearchRun.lease_expires_at <= now,
                )
                .order_by(
                    ResearchRun.lease_expires_at,
                    ResearchRun.id,
                )
                .limit(settings.research_worker_batch_size)
                .with_for_update(skip_locked=True)
            )

            result = await session.scalars(statement)
            runs = list(result.all())

            for run in runs:
                run.lease_token = None
                run.lease_expires_at = None

                if run.attempt_count >= settings.research_worker_max_attempts:
                    run.status = "failed"
                    run.failure_code = "lease_expired"
                    run.failure_message = (
                        "Research execution lease expired after the maximum number of attempts."
                    )
                    run.completed_at = now
                else:
                    run.status = "queued"
                    run.next_attempt_at = now + timedelta(
                        seconds=calculate_retry_delay(
                            run.attempt_count,
                            settings=settings,
                        )
                    )
                    run.failure_code = "lease_expired"
                    run.failure_message = (
                        "Research execution lease expired; the run was returned to the queue."
                    )
                    run.completed_at = None

                recovered_count += 1

    if recovered_count:
        logger.warning(
            "Recovered %s expired research lease(s).",
            recovered_count,
        )

    return recovered_count


async def _maintain_research_lease(
    research_run_id: UUID,
    lease_token: UUID,
    stop_event: asyncio.Event,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Renew a research lease periodically."""

    interval = settings.research_worker_heartbeat_interval_seconds

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=interval,
            )
        except TimeoutError:
            renewed = await renew_research_lease(
                research_run_id,
                lease_token,
                settings=settings,
                session_factory=session_factory,
            )

            if not renewed:
                raise ResearchLeaseLostError("Research execution lease was lost.")


async def _cancel_task_bounded(
    task: asyncio.Task,
    *,
    task_name: str,
    timeout_seconds: float = 5.0,
) -> bool:
    """Cancel a task without allowing cleanup to block indefinitely."""

    if task.done():
        return True

    task.cancel()

    _, pending = await asyncio.wait(
        {task},
        timeout=timeout_seconds,
    )

    if pending:
        logger.error(
            "Research worker task ignored cancellation: task_name=%s",
            task_name,
        )
        return False

    return True


async def _research_with_heartbeat(
    research_run: ClaimedResearchRun,
    provider: ResearchProvider,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> Sequence:
    """Run the provider with a timeout and an active lease heartbeat."""

    stop_event = asyncio.Event()

    provider_task = asyncio.create_task(
        provider.research(research_run.topic),
        name=f"research-provider-{research_run.id}",
    )

    heartbeat_task = asyncio.create_task(
        _maintain_research_lease(
            research_run.id,
            research_run.lease_token,
            stop_event,
            settings=settings,
            session_factory=session_factory,
        ),
        name=f"research-heartbeat-{research_run.id}",
    )

    try:
        done, _ = await asyncio.wait(
            {provider_task, heartbeat_task},
            timeout=settings.research_worker_provider_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            provider_stopped = await _cancel_task_bounded(
                provider_task,
                task_name=provider_task.get_name(),
            )
            if not provider_stopped:
                raise ResearchProviderCancellationError(
                    "Research provider ignored cancellation after its timeout."
                )
            raise TimeoutError("Research provider execution timed out.")

        if heartbeat_task in done:
            # A heartbeat that finishes before the provider means that lease
            # maintenance stopped unexpectedly or ownership was lost.
            heartbeat_error = (
                heartbeat_task.exception() if not heartbeat_task.cancelled() else None
            )
            provider_stopped = await _cancel_task_bounded(
                provider_task,
                task_name=provider_task.get_name(),
            )
            if not provider_stopped:
                raise ResearchProviderCancellationError(
                    "Research provider ignored cancellation after lease heartbeat stopped."
                )

            if heartbeat_error is not None:
                raise heartbeat_error

            raise ResearchLeaseLostError("Research lease heartbeat stopped unexpectedly.")

        return await provider_task

    except asyncio.CancelledError as exc:
        provider_stopped = await _cancel_task_bounded(
            provider_task,
            task_name=provider_task.get_name(),
        )
        if not provider_stopped:
            raise ResearchProviderCancellationError(
                "Research provider ignored cancellation during worker shutdown."
            ) from exc
        raise

    finally:
        stop_event.set()

        await _cancel_task_bounded(
            heartbeat_task,
            task_name=heartbeat_task.get_name(),
        )


async def _record_research_failure(
    research_run: ClaimedResearchRun,
    error: Exception,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    permanent: bool = False,
    override_failure_code: str | None = None,
    override_failure_message: str | None = None,
) -> bool:
    """
    Record a failure only if this worker still owns an active lease.

    Error details are deliberately not persisted because provider exceptions
    can contain credentials, URLs, or other sensitive information.
    """

    terminal = permanent or research_run.attempt_count >= settings.research_worker_max_attempts

    failure_code = override_failure_code or (
        "invalid_research_result"
        if permanent
        else f"research_{type(error).__name__.lower()}"[:80]
    )

    failure_message = override_failure_message or (
        "The research provider returned invalid data."
        if permanent
        else "Research execution failed. The run will be retried if attempts remain."
    )

    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(select(func.clock_timestamp()))

            if now is None:
                raise RuntimeError("Could not obtain the database clock.")

            if terminal:
                new_status = "failed"
                next_attempt_at = now
                completed_at = now
            else:
                new_status = "queued"
                next_attempt_at = now + timedelta(
                    seconds=calculate_retry_delay(
                        research_run.attempt_count,
                        settings=settings,
                    )
                )
                completed_at = None

            result = await session.execute(
                update(ResearchRun)
                .where(
                    ResearchRun.id == research_run.id,
                    ResearchRun.status == "researching",
                    ResearchRun.lease_token == research_run.lease_token,
                    ResearchRun.lease_expires_at > func.clock_timestamp(),
                )
                .values(
                    status=new_status,
                    next_attempt_at=next_attempt_at,
                    lease_token=None,
                    lease_expires_at=None,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    completed_at=completed_at,
                )
            )

            updated = result.rowcount == 1

    if updated and terminal:
        await enqueue_operations_alert(
            session_factory,
            organization_id=research_run.organization_id,
            subject="Aurora Studio: research run failed",
            body=(
                "A research run reached a terminal failure and will not be retried.\n\n"
                f"Run ID: {research_run.id}\n"
                f"Failure code: {failure_code}\n"
                f"Status: failed\n\n"
                "Open the Aurora Studio workspace, inspect the run's failure details, "
                "and retry only after resolving the underlying issue."
            ),
            settings=settings,
        )
    return updated


async def process_research_run(
    research_run: ClaimedResearchRun,
    provider: ResearchProvider,
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """Execute one claimed research run."""

    settings = settings or get_settings()
    session_factory = session_factory or get_session_factory()

    try:
        # Renew before starting the provider operation.
        renewed = await renew_research_lease(
            research_run.id,
            research_run.lease_token,
            settings=settings,
            session_factory=session_factory,
        )

        if not renewed:
            raise ResearchLeaseLostError("Research lease was lost before provider execution.")

        if research_run.organization_id is None or research_run.requested_by_user_id is None:
            raise ValueError("Claimed research run is missing trusted tenant or actor context.")
        audited_provider = AuditedResearchProvider(
            provider,
            organization_id=research_run.organization_id,
            actor_id=research_run.requested_by_user_id,
            research_run_id=research_run.id,
            attempt_count=research_run.attempt_count,
            session_factory=session_factory,
        )
        documents = tuple(
            await _research_with_heartbeat(
                research_run,
                audited_provider,
                settings=settings,
                session_factory=session_factory,
            )
        )

        if not documents:
            raise ValueError("Research provider returned no documents.")

        # Ensure the lease is still active before beginning persistence.
        # _persist_documents performs its own ownership check.
        renewed = await renew_research_lease(
            research_run.id,
            research_run.lease_token,
            settings=settings,
            session_factory=session_factory,
        )

        if not renewed:
            raise ResearchLeaseLostError("Research lease was lost before persistence.")

        source_count, evidence_count = await _persist_documents(
            session_factory,
            research_run.id,
            documents,
            lease_token=research_run.lease_token,
        )

        logger.info(
            "Research run completed: run_id=%s sources=%s evidence=%s",
            research_run.id,
            source_count,
            evidence_count,
        )

        return True

    except asyncio.CancelledError:
        raise

    except ResearchProviderCancellationError:
        # The provider task may still be executing. Do not schedule a retry
        # while it remains alive; the standalone worker must fail-stop so
        # process exit terminates the uncooperative operation.
        raise

    except ResearchLeaseLostError:
        logger.warning(
            "Research worker lost lease: run_id=%s",
            research_run.id,
        )
        return False

    except ToolAuthorizationError as exc:
        logger.warning(
            "Research run authorization denied: run_id=%s",
            research_run.id,
        )
        await _record_research_failure(
            research_run,
            exc,
            settings=settings,
            session_factory=session_factory,
            permanent=True,
            override_failure_code="authorization_denied",
            override_failure_message=(
                "The requester is no longer authorized to run research for this organization."
            ),
        )
        return False

    except ValueError as exc:
        # Invalid provider output is not expected to become valid by retrying.
        logger.warning(
            "Research run returned invalid data: run_id=%s error_type=%s",
            research_run.id,
            type(exc).__name__,
        )

        await _record_research_failure(
            research_run,
            exc,
            settings=settings,
            session_factory=session_factory,
            permanent=True,
        )

        return False

    except Exception as exc:
        # Do not expose potentially sensitive provider exception details.
        logger.error(
            "Research execution failed: run_id=%s error_type=%s",
            research_run.id,
            type(exc).__name__,
        )

        await _record_research_failure(
            research_run,
            exc,
            settings=settings,
            session_factory=session_factory,
        )

        return False
