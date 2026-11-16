import asyncio
import os
import select
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.workers import run_research_worker as worker_entrypoint
from app.workers.research_worker import (
    ResearchProviderCancellationError,
    _cancel_task_bounded,
)


def test_drain_deadline_cancels_inflight_research_tasks(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_research(
        research_run,
        provider,
        *,
        settings,
        session_factory,
    ):
        started.set()

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        worker_entrypoint,
        "process_research_run",
        slow_research,
    )

    async def run_test():
        research_run = SimpleNamespace(id=uuid4())

        await worker_entrypoint._process_batch(
            [research_run],
            provider=object(),
            settings=None,
            session_factory=None,
            deadline=time.monotonic() + 0.1,
        )

    asyncio.run(run_test())

    assert started.is_set()
    assert cancelled.is_set()


def test_normal_batch_waits_for_inflight_research_tasks(monkeypatch):
    completed = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_research(
        research_run,
        provider,
        *,
        settings,
        session_factory,
    ):
        try:
            await asyncio.sleep(0.05)
            completed.set()
            return True
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        worker_entrypoint,
        "process_research_run",
        slow_research,
    )

    async def run_test():
        research_run = SimpleNamespace(id=uuid4())

        await worker_entrypoint._process_batch(
            [research_run],
            provider=object(),
            settings=None,
            session_factory=None,
            deadline=None,
        )

    asyncio.run(run_test())

    assert completed.is_set()
    assert not cancelled.is_set()


def test_sigterm_allows_active_research_to_finish(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    stop_event = asyncio.Event()

    async def slow_research(
        research_run,
        provider,
        *,
        settings,
        session_factory,
    ):
        started.set()
        await release.wait()
        completed.set()
        return True

    monkeypatch.setattr(
        worker_entrypoint,
        "process_research_run",
        slow_research,
    )

    async def run_test():
        research_run = SimpleNamespace(id=uuid4())
        batch_task = asyncio.create_task(
            worker_entrypoint._process_batch(
                [research_run],
                provider=object(),
                settings=None,
                session_factory=None,
                deadline=None,
                stop_event=stop_event,
                shutdown_timeout_seconds=1,
            )
        )

        await started.wait()
        stop_event.set()
        await asyncio.sleep(0.05)
        assert not batch_task.done()

        release.set()
        await batch_task

    asyncio.run(run_test())

    assert completed.is_set()


def test_sigterm_cancels_active_research_after_shutdown_timeout(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    stop_event = asyncio.Event()

    async def slow_research(
        research_run,
        provider,
        *,
        settings,
        session_factory,
    ):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        worker_entrypoint,
        "process_research_run",
        slow_research,
    )

    async def run_test():
        research_run = SimpleNamespace(id=uuid4())
        batch_task = asyncio.create_task(
            worker_entrypoint._process_batch(
                [research_run],
                provider=object(),
                settings=None,
                session_factory=None,
                deadline=None,
                stop_event=stop_event,
                shutdown_timeout_seconds=0.05,
            )
        )

        await started.wait()
        stop_event.set()
        await batch_task

    asyncio.run(run_test())

    assert cancelled.is_set()


def test_parent_cancellation_cancels_and_awaits_research_tasks(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def slow_research(
        research_run,
        provider,
        *,
        settings,
        session_factory,
    ):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.sleep(0)
            cleanup_finished.set()
            raise

    monkeypatch.setattr(
        worker_entrypoint,
        "process_research_run",
        slow_research,
    )

    async def run_test():
        research_run = SimpleNamespace(id=uuid4())
        batch_task = asyncio.create_task(
            worker_entrypoint._process_batch(
                [research_run],
                provider=object(),
                settings=None,
                session_factory=None,
                deadline=None,
            )
        )

        await started.wait()
        batch_task.cancel()

        try:
            await batch_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("The parent batch task should remain cancelled.")

        assert cancelled.is_set()
        assert cleanup_finished.is_set()

    asyncio.run(run_test())


def test_shutdown_does_not_wait_indefinitely_for_cancellation_cleanup(monkeypatch):
    started = asyncio.Event()
    cancellation_received = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    research_run = SimpleNamespace(id=uuid4())

    async def cancellation_resistant_research(
        run,
        provider,
        *,
        settings,
        session_factory,
    ):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_received.set()
            await release_cleanup.wait()
            cleanup_finished.set()
            raise

    monkeypatch.setattr(
        worker_entrypoint,
        "process_research_run",
        cancellation_resistant_research,
    )

    async def run_test():
        stop_event = asyncio.Event()
        batch_task = asyncio.create_task(
            worker_entrypoint._process_batch(
                [research_run],
                provider=object(),
                settings=None,
                session_factory=None,
                deadline=None,
                stop_event=stop_event,
                shutdown_timeout_seconds=0.05,
            )
        )

        await started.wait()
        stop_event.set()

        await asyncio.wait_for(batch_task, timeout=0.5)

        assert cancellation_received.is_set()
        assert not cleanup_finished.is_set()

        research_task = next(
            task
            for task in asyncio.all_tasks()
            if task.get_name() == f"research-run-{research_run.id}"
        )
        assert not research_task.done()

        release_cleanup.set()

        try:
            await asyncio.wait_for(research_task, timeout=0.5)
        except asyncio.CancelledError:
            pass

        assert research_task.cancelled()
        assert cleanup_finished.is_set()

    asyncio.run(run_test())


def test_provider_task_cleanup_is_bounded():
    async def run_test():
        cancellation_received = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()

        async def cancellation_resistant_provider():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_received.set()
                await release_cleanup.wait()
                cleanup_finished.set()
                raise

        task = asyncio.create_task(cancellation_resistant_provider())
        await asyncio.sleep(0)

        stopped = await _cancel_task_bounded(
            task,
            task_name="research-provider-test",
            timeout_seconds=0.05,
        )

        assert stopped is False
        assert cancellation_received.is_set()
        assert not cleanup_finished.is_set()
        assert not task.done()

        release_cleanup.set()
        result = await asyncio.gather(task, return_exceptions=True)

        assert isinstance(result[0], asyncio.CancelledError)
        assert cleanup_finished.is_set()

    asyncio.run(run_test())


def test_heartbeat_task_cleanup_is_bounded():
    async def run_test():
        cancellation_received = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()

        async def cancellation_resistant_heartbeat():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_received.set()
                await release_cleanup.wait()
                cleanup_finished.set()
                raise

        task = asyncio.create_task(cancellation_resistant_heartbeat())
        await asyncio.sleep(0)

        stopped = await _cancel_task_bounded(
            task,
            task_name="research-heartbeat-test",
            timeout_seconds=0.05,
        )

        assert stopped is False
        assert cancellation_received.is_set()
        assert not cleanup_finished.is_set()
        assert not task.done()

        release_cleanup.set()
        result = await asyncio.gather(task, return_exceptions=True)

        assert isinstance(result[0], asyncio.CancelledError)
        assert cleanup_finished.is_set()

    asyncio.run(run_test())


def test_drain_recovers_claims_processes_and_exits(monkeypatch):
    research_run = SimpleNamespace(id=uuid4())
    settings = SimpleNamespace(
        research_worker_heartbeat_interval_seconds=30,
        research_worker_poll_interval_seconds=1,
        research_worker_shutdown_timeout_seconds=1,
        research_worker_batch_size=1,
    )
    recovered_calls = []
    claim_calls = []
    processed_runs = []
    dispatch_calls = []

    async def recover_expired(**kwargs):
        recovered_calls.append(kwargs)
        return 0

    async def dispatch_due(**kwargs):
        dispatch_calls.append(kwargs)
        return SimpleNamespace(dispatched=0, disabled=0, skipped=0, busy=0)

    async def claim_batch(worker_id, **kwargs):
        claim_calls.append((worker_id, kwargs))
        if len(claim_calls) == 1:
            return [research_run]
        return []

    async def process_run(run, provider, **kwargs):
        processed_runs.append(run.id)
        return True

    monkeypatch.setattr(worker_entrypoint, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_entrypoint, "get_session_factory", lambda: object())
    monkeypatch.setattr(worker_entrypoint, "recover_expired_research_leases", recover_expired)
    monkeypatch.setattr(
        worker_entrypoint, "dispatch_due_channel_schedules_once", dispatch_due
    )
    monkeypatch.setattr(worker_entrypoint, "claim_research_batch", claim_batch)
    monkeypatch.setattr(worker_entrypoint, "process_research_run", process_run)

    asyncio.run(
        worker_entrypoint.run_research_worker(
            drain=True,
            max_runtime_seconds=5,
        )
    )

    assert len(recovered_calls) == 2
    assert len(claim_calls) == 2
    assert len(dispatch_calls) == 2
    assert processed_runs == [research_run.id]


def test_worker_stops_before_claim_when_stop_requested(monkeypatch):
    settings = SimpleNamespace(
        research_worker_heartbeat_interval_seconds=30,
        research_worker_poll_interval_seconds=1,
        research_worker_shutdown_timeout_seconds=1,
    )
    stop_event = asyncio.Event()
    calls = []

    async def recover_expired(**kwargs):
        calls.append("recover")
        stop_event.set()
        return 0

    async def claim_batch(worker_id, **kwargs):
        calls.append("claim")
        return []

    monkeypatch.setattr(worker_entrypoint, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_entrypoint, "get_session_factory", lambda: object())
    monkeypatch.setattr(worker_entrypoint, "recover_expired_research_leases", recover_expired)
    monkeypatch.setattr(worker_entrypoint, "claim_research_batch", claim_batch)

    asyncio.run(
        worker_entrypoint.run_research_worker(
            drain=False,
            stop_event=stop_event,
        )
    )

    assert calls == ["recover"]


def test_worker_cleanup_disposes_engine_when_worker_fails(monkeypatch):
    disposed = []

    async def failing_worker(**kwargs):
        raise RuntimeError("simulated worker failure")

    async def dispose_engine():
        disposed.append(True)

    monkeypatch.setattr(worker_entrypoint, "run_research_worker", failing_worker)
    monkeypatch.setattr(worker_entrypoint, "dispose_engine", dispose_engine)

    async def run_test():
        try:
            await worker_entrypoint.run_worker_with_cleanup()
        except RuntimeError as exc:
            assert str(exc) == "simulated worker failure"
        else:
            raise AssertionError("The worker exception should propagate.")

    asyncio.run(run_test())

    assert disposed == [True]


def test_main_exits_cleanly_after_sigterm():
    api_dir = Path(__file__).resolve().parents[1]

    child_script = textwrap.dedent(
        """
        import argparse
        import asyncio
        from types import SimpleNamespace

        from app.workers import run_research_worker as worker

        async def controlled_worker(*, drain, max_runtime_seconds, stop_event):
            print("WORKER_READY", flush=True)
            await stop_event.wait()

        worker.get_settings = lambda: SimpleNamespace(log_level="INFO")
        worker._parse_args = lambda: argparse.Namespace(
            drain=False,
            max_runtime_seconds=240,
        )
        worker.run_worker_with_cleanup = controlled_worker

        worker.main()
        """
    )

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(api_dir) if not existing_pythonpath else str(api_dir) + os.pathsep + existing_pythonpath
    )

    process = subprocess.Popen(
        [sys.executable, "-c", child_script],
        cwd=api_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 5)

        assert readable, "Worker subprocess did not become ready in time."
        assert process.stdout.readline().strip() == "WORKER_READY"

        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=5)

        assert process.returncode == 0, (
            f"Worker did not exit cleanly after SIGTERM. Output: {output}"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_worker_process_exits_with_cancellation_resistant_task():
    script = textwrap.dedent(
        """
        import asyncio

        from app.workers.run_research_worker import _run_worker_event_loop


        async def main():
            async def cancellation_resistant():
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    while True:
                        try:
                            await asyncio.sleep(1)
                        except asyncio.CancelledError:
                            continue

            task = asyncio.create_task(
                cancellation_resistant(),
                name="deliberately-cancellation-resistant-provider",
            )
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)


        _run_worker_event_loop(main(), pending_task_timeout_seconds=0.1)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    assert "cancellation-resistant tasks" in result.stderr


def test_batch_propagates_cancellation_resistant_provider_failure(monkeypatch):
    async def fatal_research(
        research_run,
        provider,
        *,
        settings,
        session_factory,
    ):
        raise ResearchProviderCancellationError(
            "Synthetic provider ignored cancellation."
        )

    monkeypatch.setattr(
        worker_entrypoint,
        "process_research_run",
        fatal_research,
    )

    async def run_test():
        try:
            await worker_entrypoint._process_batch(
                [SimpleNamespace(id=uuid4())],
                provider=object(),
                settings=None,
                session_factory=None,
                deadline=None,
            )
        except ResearchProviderCancellationError:
            return
        raise AssertionError("Fatal provider cancellation must stop the worker batch.")

    asyncio.run(run_test())


def test_drain_processes_real_mock_research_run_and_persists_evidence():
    """Exercise the production worker entrypoint against the isolated test DB."""
    from datetime import UTC, datetime

    from sqlalchemy import delete, func, select

    from app.database.engine import get_session_factory
    from app.models.evidence_item import EvidenceItem
    from app.models.membership import OrganizationMember
    from app.models.organization import Organization
    from app.models.research_run import ResearchRun
    from app.models.research_source import ResearchSource
    from app.models.user import User

    async def seed_run():
        async with get_session_factory()() as session:
            user = User(
                email=f"research-worker-e2e-{uuid4().hex}@example.com",
                display_name="Research Worker E2E",
                email_verified_at=datetime.now(UTC),
            )
            session.add(user)
            await session.flush()

            organization = Organization(
                name=f"Research Worker E2E {uuid4().hex[:8]}",
                slug=f"research-worker-e2e-{uuid4().hex}",
                created_by_user_id=user.id,
            )
            session.add(organization)
            await session.flush()

            session.add(
                OrganizationMember(
                    organization_id=organization.id,
                    user_id=user.id,
                    role="owner",
                    status="active",
                    joined_at=datetime.now(UTC),
                )
            )
            run = ResearchRun(
                organization_id=organization.id,
                requested_by_user_id=user.id,
                topic="Synthetic end-to-end worker smoke test",
                status="queued",
            )
            session.add(run)
            await session.commit()
            return user.id, organization.id, run.id

    async def verify_and_cleanup(user_id, organization_id, run_id):
        async with get_session_factory()() as session:
            run = await session.get(ResearchRun, run_id)
            assert run is not None
            assert run.status == "completed"
            assert run.attempt_count == 1
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
            assert source_count == 2
            assert evidence_count == 2

    async def cleanup(user_id, organization_id, run_id):
        async with get_session_factory()() as session:
            async with session.begin():
                source_ids = select(ResearchSource.id).where(
                    ResearchSource.research_run_id == run_id
                )
                await session.execute(
                    delete(EvidenceItem).where(
                        EvidenceItem.research_source_id.in_(source_ids)
                    )
                )
                await session.execute(
                    delete(ResearchSource).where(
                        ResearchSource.research_run_id == run_id
                    )
                )
                await session.execute(delete(ResearchRun).where(ResearchRun.id == run_id))
                await session.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.organization_id == organization_id
                    )
                )
                await session.execute(
                    delete(Organization).where(Organization.id == organization_id)
                )
                await session.execute(delete(User).where(User.id == user_id))

    async def run_test():
        user_id, organization_id, run_id = await seed_run()
        settings = worker_entrypoint.get_settings()
        previous_research_provider = settings.research_provider
        previous_trend_provider = settings.trend_discovery_provider
        previous_trend_feeds = settings.trend_discovery_feeds
        previous_batch_size = settings.research_worker_batch_size
        previous_concurrency = settings.research_worker_max_concurrency
        previous_poll_interval = settings.research_worker_poll_interval_seconds
        try:
            # Make this worker smoke test independent of optional .env feeds.
            settings.research_provider = "mock"
            settings.trend_discovery_provider = "none"
            settings.trend_discovery_feeds = []
            settings.research_worker_batch_size = 1
            settings.research_worker_max_concurrency = 1
            settings.research_worker_poll_interval_seconds = 1

            await worker_entrypoint.run_research_worker(
                drain=True,
                max_runtime_seconds=10,
            )
            await verify_and_cleanup(user_id, organization_id, run_id)
        finally:
            await cleanup(user_id, organization_id, run_id)
            settings.research_worker_batch_size = previous_batch_size
            settings.research_worker_max_concurrency = previous_concurrency
            settings.research_worker_poll_interval_seconds = previous_poll_interval
            settings.research_provider = previous_research_provider
            settings.trend_discovery_provider = previous_trend_provider
            settings.trend_discovery_feeds = previous_trend_feeds

    asyncio.run(run_test())

def test_research_provider_factory_keeps_mock_default_and_selects_explicit_modes():
    from app.research.multi_source import MultiSourceResearchProvider
    from app.research.providers.crossref import CrossrefResearchProvider
    from app.research.providers.http import HttpResearchProvider
    from app.research.providers.mock import MockResearchProvider
    from app.research.providers.searxng_research import SearXNGResearchProvider

    assert isinstance(
        worker_entrypoint._build_research_provider(SimpleNamespace(research_provider="mock")),
        MockResearchProvider,
    )
    assert isinstance(
        worker_entrypoint._build_research_provider(
            SimpleNamespace(research_provider="wikipedia_http")
        ),
        HttpResearchProvider,
    )
    assert isinstance(
        worker_entrypoint._build_research_provider(
            SimpleNamespace(research_provider="crossref")
        ),
        CrossrefResearchProvider,
    )
    multi_source = worker_entrypoint._build_research_provider(
        SimpleNamespace(research_provider="multi_source")
    )
    assert isinstance(multi_source, MultiSourceResearchProvider)
    assert len(multi_source._providers) == 2
    assert isinstance(multi_source._providers[0], HttpResearchProvider)
    assert isinstance(multi_source._providers[1], CrossrefResearchProvider)
    searxng_sources = worker_entrypoint._build_research_provider(SimpleNamespace(
        research_provider="multi_source_searxng",
        research_searxng_base_url="https://search.example.org",
        research_egress_proxy_url=None,
    ))
    assert isinstance(searxng_sources, MultiSourceResearchProvider)
    assert len(searxng_sources._providers) == 3
    assert isinstance(searxng_sources._providers[2], SearXNGResearchProvider)
