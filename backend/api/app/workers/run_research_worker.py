import argparse
import asyncio
import logging
import os
import signal
import time

from app.core.config import get_settings
from app.database.engine import dispose_engine, get_session_factory
from app.research.multi_source import MultiSourceResearchProvider
from app.research.providers.crossref import CrossrefResearchProvider
from app.research.providers.http import HttpResearchProvider
from app.research.providers.mock import MockResearchProvider
from app.research.providers.searxng_research import SearXNGResearchProvider
from app.services.channel_schedule_dispatcher import dispatch_due_channel_schedules_once
from app.workers.research_worker import (
    ResearchProviderCancellationError,
    claim_research_batch,
    create_worker_id,
    process_research_run,
    recover_expired_research_leases,
)

logger = logging.getLogger(__name__)


async def _cancel_and_wait(
    tasks: set[asyncio.Task],
    *,
    timeout_seconds: float,
    reason: str,
) -> set[asyncio.Task]:
    """Cancel tasks and wait only for a bounded cleanup period."""

    pending = {task for task in tasks if not task.done()}
    for task in pending:
        task.cancel()

    if not pending:
        return set()

    _, still_pending = await asyncio.wait(
        pending,
        timeout=timeout_seconds,
    )

    if still_pending:
        logger.error(
            "Research tasks did not stop within the cleanup timeout: "
            "reason=%s pending_count=%s task_names=%s",
            reason,
            len(still_pending),
            sorted(task.get_name() for task in still_pending),
        )

    return still_pending


async def _process_batch(
    runs,
    provider,
    *,
    settings,
    session_factory,
    deadline: float | None,
    stop_event: asyncio.Event | None = None,
    shutdown_timeout_seconds: int = 120,
) -> None:
    # Process a batch until completion or its applicable shutdown deadline.

    if shutdown_timeout_seconds <= 0:
        raise ValueError("Shutdown timeout must be positive.")

    tasks = [
        asyncio.create_task(
            process_research_run(
                run,
                provider,
                settings=settings,
                session_factory=session_factory,
            ),
            name=f"research-run-{run.id}",
        )
        for run in runs
    ]

    stop_waiter = (
        asyncio.create_task(stop_event.wait(), name="research-worker-stop-waiter")
        if stop_event is not None
        else None
    )
    effective_deadline = deadline
    shutdown_requested = False
    cleanup_attempted = False

    try:
        pending_tasks = set(tasks)

        while pending_tasks:
            waiters = set(pending_tasks)
            if stop_waiter is not None and not stop_waiter.done():
                waiters.add(stop_waiter)

            timeout = (
                None
                if effective_deadline is None
                else max(0.0, effective_deadline - time.monotonic())
            )

            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            pending_tasks = {task for task in tasks if not task.done()}

            if stop_waiter is not None and stop_waiter in done:
                if not shutdown_requested:
                    shutdown_requested = True
                    if effective_deadline is None:
                        effective_deadline = time.monotonic() + shutdown_timeout_seconds
                        logger.info(
                            "Research worker shutdown requested; allowing "
                            "up to %s seconds for active runs to finish.",
                            shutdown_timeout_seconds,
                        )

            if not pending_tasks:
                break

            if effective_deadline is not None and time.monotonic() >= effective_deadline:
                if shutdown_requested:
                    logger.warning(
                        "Research worker shutdown deadline reached; "
                        "cancelling %s in-flight run(s). Their leases will "
                        "be recovered after expiry.",
                        len(pending_tasks),
                    )
                else:
                    logger.warning(
                        "Research drain deadline reached; cancelling %s "
                        "in-flight run(s). Their leases will be recovered "
                        "after expiry.",
                        len(pending_tasks),
                    )

                for task in pending_tasks:
                    task.cancel()
                break

        if any(not task.done() for task in tasks):
            cleanup_attempted = True
            await _cancel_and_wait(
                set(tasks),
                timeout_seconds=min(5.0, float(shutdown_timeout_seconds)),
                reason="batch deadline",
            )

        # Do not await tasks that ignored cancellation. Their leases will
        # expire and be recovered by a later worker.
        results = [
            asyncio.CancelledError() if not task.done() or task.cancelled() else task.exception()
            for task in tasks
        ]

        fatal_provider_error = next(
            (
                result
                for result in results
                if isinstance(result, ResearchProviderCancellationError)
            ),
            None,
        )
        if fatal_provider_error is not None:
            raise fatal_provider_error

    finally:
        # Parent cancellation must not leave child tasks running indefinitely.
        if not cleanup_attempted:
            await _cancel_and_wait(
                set(tasks),
                timeout_seconds=min(5.0, float(shutdown_timeout_seconds)),
                reason="parent cancellation or batch cleanup",
            )

        if stop_waiter is not None:
            if not stop_waiter.done():
                stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)

    for run, result in zip(runs, results, strict=True):
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                logger.info(
                    "Research task cancelled: run_id=%s",
                    run.id,
                )
            else:
                logger.error(
                    "Unexpected research task failure: run_id=%s error_type=%s",
                    run.id,
                    type(result).__name__,
                )


def _build_research_provider(settings):
    """Resolve only explicitly configured provider modes; mock remains the default."""

    provider_name = getattr(settings, "research_provider", "mock")
    proxy_url = getattr(settings, "research_egress_proxy_url", None)
    if provider_name == "wikipedia_http":
        return HttpResearchProvider(proxy_url=proxy_url)
    if provider_name == "crossref":
        return CrossrefResearchProvider(proxy_url=proxy_url)
    if provider_name == "multi_source":
        return MultiSourceResearchProvider((
            HttpResearchProvider(proxy_url=proxy_url),
            CrossrefResearchProvider(proxy_url=proxy_url),
        ))
    if provider_name == "multi_source_searxng":
        base_url = getattr(settings, "research_searxng_base_url", None)
        if not base_url:
            raise ValueError(
                "Research SearXNG mode requires RESEARCH_SEARXNG_BASE_URL."
            )
        return MultiSourceResearchProvider((
            HttpResearchProvider(proxy_url=proxy_url),
            CrossrefResearchProvider(proxy_url=proxy_url),
            SearXNGResearchProvider(base_url=base_url, proxy_url=proxy_url),
        ))
    return MockResearchProvider()


async def run_research_worker(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 240,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Process research runs continuously or as a bounded drain."""

    if max_runtime_seconds <= 0:
        raise ValueError("Maximum worker runtime must be positive.")

    settings = get_settings()
    session_factory = get_session_factory()
    provider = _build_research_provider(settings)
    worker_id = create_worker_id()

    stop_event = stop_event or asyncio.Event()
    deadline = time.monotonic() + max_runtime_seconds if drain else None
    next_heartbeat = time.monotonic()

    logger.info(
        "Research worker started: worker_id=%s mode=%s",
        worker_id,
        "drain" if drain else "continuous",
    )

    try:
        while not stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                logger.info("Research worker drain reached its runtime limit.")
                break

            try:
                monotonic_now = time.monotonic()

                if not drain and monotonic_now >= next_heartbeat:
                    logger.info(
                        "Research worker heartbeat: worker_id=%s",
                        worker_id,
                    )
                    next_heartbeat = (
                        monotonic_now + settings.research_worker_heartbeat_interval_seconds
                    )

                recovered = await recover_expired_research_leases(
                    settings=settings,
                    session_factory=session_factory,
                )

                if recovered:
                    logger.warning(
                        "Research worker recovered %s expired lease(s).",
                        recovered,
                    )

                if stop_event.is_set():
                    break

                dispatch_summary = await dispatch_due_channel_schedules_once(
                    batch_size=settings.research_worker_batch_size,
                    session_factory=session_factory,
                )
                if dispatch_summary.dispatched or dispatch_summary.disabled:
                    logger.info(
                        "Channel schedules processed: dispatched=%s disabled=%s "
                        "skipped=%s busy=%s",
                        dispatch_summary.dispatched,
                        dispatch_summary.disabled,
                        dispatch_summary.skipped,
                        dispatch_summary.busy,
                    )

                runs = await claim_research_batch(
                    worker_id,
                    settings=settings,
                    session_factory=session_factory,
                )

                if runs:
                    logger.info(
                        "Research worker claimed %s run(s).",
                        len(runs),
                    )

                    await _process_batch(
                        runs,
                        provider,
                        settings=settings,
                        session_factory=session_factory,
                        deadline=deadline,
                        stop_event=stop_event,
                        shutdown_timeout_seconds=(
                            settings.research_worker_shutdown_timeout_seconds
                        ),
                    )

                    if deadline is not None and time.monotonic() >= deadline:
                        logger.info("Research worker drain finished at its deadline.")
                        break

                    continue

                if drain:
                    logger.info("Research worker drain complete: no currently available runs.")
                    break

                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.research_worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass

            except asyncio.CancelledError:
                raise

            except ResearchProviderCancellationError:
                # A provider still running after cancellation makes retrying
                # in this process unsafe. Let the process-owned loop close so
                # process exit terminates that provider task.
                logger.exception(
                    "Research provider ignored cancellation; stopping worker process."
                )
                raise

            except Exception:
                logger.exception("Research worker iteration failed.")

                if drain:
                    raise

                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.research_worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass

    finally:
        logger.info(
            "Research worker stopped: worker_id=%s",
            worker_id,
        )


async def run_worker_with_cleanup(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 240,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Always dispose the cached database engine on shutdown."""

    try:
        await run_research_worker(
            drain=drain,
            max_runtime_seconds=max_runtime_seconds,
            stop_event=stop_event,
        )
    finally:
        await dispose_engine()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aurora Studio research worker.")

    parser.add_argument(
        "--drain",
        action="store_true",
        help="Process available research runs and exit.",
    )

    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=240,
        help="Maximum drain runtime in seconds (default: 240).",
    )

    return parser.parse_args()


def _run_worker_event_loop(
    coroutine,
    *,
    pending_task_timeout_seconds: float = 5.0,
) -> None:
    """Run the worker and close the process-owned loop after bounded cleanup.

    asyncio.run() waits for every remaining task during shutdown. A provider
    coroutine that suppresses cancellation can therefore prevent the worker
    process from exiting. This runner gives pending tasks a final bounded
    cleanup window, then closes the loop. Because this loop belongs exclusively
    to the worker process, closing it and exiting the process terminates any
    remaining in-process provider operations; their database leases are
    recovered by a later worker.
    """

    if pending_task_timeout_seconds <= 0:
        raise ValueError("Pending-task cleanup timeout must be positive.")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    still_pending: set[asyncio.Task] = set()

    try:
        loop.run_until_complete(coroutine)
    finally:
        pending = {task for task in asyncio.all_tasks(loop) if not task.done()}

        if pending:
            for task in pending:
                task.cancel()

            _, still_pending = loop.run_until_complete(
                asyncio.wait(
                    pending,
                    timeout=pending_task_timeout_seconds,
                )
            )

            if still_pending:
                logger.critical(
                    "Research worker is exiting with cancellation-resistant tasks; "
                    "the process boundary will terminate them: task_names=%s",
                    sorted(task.get_name() for task in still_pending),
                )

        loop.close()
        asyncio.set_event_loop(None)

        if still_pending:
            # Python may wait for executor threads during interpreter shutdown.
            # A hard process exit is the only reliable boundary for arbitrary
            # cancellation-resistant in-process work.
            logging.shutdown()
            os._exit(1)


def main() -> None:
    """Run the worker with graceful SIGINT/SIGTERM handling."""

    args = _parse_args()

    if args.max_runtime_seconds <= 0:
        raise SystemExit("--max-runtime-seconds must be greater than zero.")

    settings = get_settings()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    async def run() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass

        await run_worker_with_cleanup(
            drain=args.drain,
            max_runtime_seconds=args.max_runtime_seconds,
            stop_event=stop_event,
        )

    try:
        _run_worker_event_loop(run())
    except KeyboardInterrupt:
        logger.info("Research worker interrupted.")


if __name__ == "__main__":
    main()
