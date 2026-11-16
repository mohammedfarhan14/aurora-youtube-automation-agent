import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.workers import run_email_outbox


def test_worker_disposes_database_engine_after_normal_shutdown(monkeypatch):
    async def stop_worker(**kwargs):
        return None

    dispose = AsyncMock()
    monkeypatch.setattr(run_email_outbox, "run_email_outbox_worker", stop_worker)
    monkeypatch.setattr(run_email_outbox, "dispose_engine", dispose)

    asyncio.run(run_email_outbox.run_worker_with_cleanup())

    dispose.assert_awaited_once()


def test_worker_disposes_database_engine_after_cancellation(monkeypatch):
    async def cancel_worker(**kwargs):
        raise asyncio.CancelledError()

    dispose = AsyncMock()
    monkeypatch.setattr(run_email_outbox, "run_email_outbox_worker", cancel_worker)
    monkeypatch.setattr(run_email_outbox, "dispose_engine", dispose)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_email_outbox.run_worker_with_cleanup())

    dispose.assert_awaited_once()


def test_worker_drains_until_no_messages_remain(monkeypatch):
    calls = {"claims": 0, "processed": []}
    monkeypatch.setattr(run_email_outbox, "get_settings", lambda: SimpleNamespace(
        email_outbox_poll_interval_seconds=0.01
    ))
    monkeypatch.setattr(run_email_outbox, "get_email_sender", lambda settings: object())
    monkeypatch.setattr(run_email_outbox, "create_worker_id", lambda: "test-worker")

    async def recover(**kwargs):
        return 0

    async def claim(worker_id, **kwargs):
        calls["claims"] += 1
        return [SimpleNamespace(id=calls["claims"])] if calls["claims"] < 3 else []

    async def process(message_id, worker_id, sender, **kwargs):
        calls["processed"].append(message_id)
        return True

    monkeypatch.setattr(run_email_outbox, "recover_expired_email_leases", recover)
    monkeypatch.setattr(run_email_outbox, "claim_email_batch", claim)
    monkeypatch.setattr(run_email_outbox, "process_email_message", process)

    asyncio.run(run_email_outbox.run_email_outbox_worker(drain=True))

    assert calls["claims"] == 3
    assert calls["processed"] == [1, 2]


def test_worker_drain_rejects_non_positive_runtime():
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(run_email_outbox.run_email_outbox_worker(drain=True, max_runtime_seconds=0))


def test_continuous_worker_stops_without_an_extra_poll(monkeypatch):
    stop_event = asyncio.Event()
    stop_event.set()
    monkeypatch.setattr(run_email_outbox, "get_settings", lambda: SimpleNamespace(
        email_outbox_poll_interval_seconds=0.01
    ))
    monkeypatch.setattr(run_email_outbox, "get_email_sender", lambda settings: object())
    monkeypatch.setattr(run_email_outbox, "create_worker_id", lambda: "test-worker")

    async def unexpected_recover(**kwargs):
        raise AssertionError("worker should stop before claiming work")

    monkeypatch.setattr(run_email_outbox, "recover_expired_email_leases", unexpected_recover)

    asyncio.run(run_email_outbox.run_email_outbox_worker(stop_event=stop_event))

def test_continuous_worker_finishes_in_flight_delivery_after_stop(monkeypatch):
    async def scenario():
        stop_event = asyncio.Event()
        delivery_started = asyncio.Event()
        finish_delivery = asyncio.Event()
        processed = []

        monkeypatch.setattr(
            run_email_outbox,
            "get_settings",
            lambda: SimpleNamespace(
                email_outbox_poll_interval_seconds=0.01,
                email_outbox_heartbeat_interval_seconds=3600,
            ),
        )
        monkeypatch.setattr(
            run_email_outbox, "get_email_sender", lambda settings: object()
        )
        monkeypatch.setattr(
            run_email_outbox, "create_worker_id", lambda: "test-worker"
        )

        async def health(**kwargs):
            return {
                "pending_count": 1,
                "available_pending_count": 1,
                "processing_count": 0,
                "sent_count": 0,
                "failed_count": 0,
                "oldest_available_pending_age_seconds": 0,
                "oldest_processing_age_seconds": None,
            }

        async def recover(**kwargs):
            return 0

        async def claim(worker_id, **kwargs):
            return [SimpleNamespace(id="message-1")]

        async def process(message_id, worker_id, sender, **kwargs):
            processed.append(message_id)
            delivery_started.set()
            await finish_delivery.wait()
            return True

        monkeypatch.setattr(run_email_outbox, "get_email_outbox_health", health)
        monkeypatch.setattr(run_email_outbox, "recover_expired_email_leases", recover)
        monkeypatch.setattr(run_email_outbox, "claim_email_batch", claim)
        monkeypatch.setattr(run_email_outbox, "process_email_message", process)

        worker = asyncio.create_task(
            run_email_outbox.run_email_outbox_worker(stop_event=stop_event)
        )
        await asyncio.wait_for(delivery_started.wait(), timeout=1)
        stop_event.set()
        await asyncio.sleep(0)
        assert not worker.done()

        finish_delivery.set()
        await asyncio.wait_for(worker, timeout=1)
        assert processed == ["message-1"]

    asyncio.run(scenario())



def test_main_wires_sigint_and_sigterm_to_graceful_stop(monkeypatch):
    import signal

    registered_handlers = {}
    worker_calls = []

    class FakeLoop:
        def add_signal_handler(self, sig, callback):
            registered_handlers[sig] = callback

    async def fake_worker(**kwargs):
        registered_handlers[signal.SIGTERM]()
        assert kwargs["stop_event"].is_set()
        worker_calls.append(kwargs)

    real_asyncio_run = asyncio.run
    monkeypatch.setattr(
        run_email_outbox,
        "_parse_args",
        lambda: SimpleNamespace(drain=False, max_runtime_seconds=240),
    )
    monkeypatch.setattr(
        run_email_outbox,
        "get_settings",
        lambda: SimpleNamespace(log_level="INFO"),
    )
    monkeypatch.setattr(
        run_email_outbox.asyncio,
        "get_running_loop",
        lambda: FakeLoop(),
    )
    monkeypatch.setattr(run_email_outbox, "run_worker_with_cleanup", fake_worker)

    def run_main(coroutine):
        async def execute():
            await coroutine

        return real_asyncio_run(execute())

    monkeypatch.setattr(run_email_outbox.asyncio, "run", run_main)

    # Invoke the registered callback inside the fake worker. This verifies
    # signal-to-stop-event wiring without sending an OS-level signal.
    run_email_outbox.main()

    assert set(registered_handlers) == {signal.SIGINT, signal.SIGTERM}
    assert len(worker_calls) == 1
    assert worker_calls[0]["stop_event"].is_set()
