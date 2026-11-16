import argparse
import asyncio
import logging
import signal
import time

from app.core.config import get_settings
from app.database.engine import dispose_engine
from app.services.email import get_email_sender
from app.workers.email_outbox_worker import (
    claim_email_batch,
    create_worker_id,
    get_email_outbox_health,
    process_email_message,
    recover_expired_email_leases,
)

logger = logging.getLogger(__name__)


async def run_email_outbox_worker(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 240,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Process the email outbox continuously or as a bounded scheduled drain."""

    if max_runtime_seconds <= 0:
        raise ValueError("Maximum worker runtime must be positive.")

    settings = get_settings()
    worker_id = create_worker_id()
    sender = get_email_sender(settings)
    stop_event = stop_event or asyncio.Event()
    deadline = time.monotonic() + max_runtime_seconds if drain else None
    next_heartbeat = time.monotonic()

    logger.info(
        "Email outbox worker started: %s (mode=%s)",
        worker_id,
        "drain" if drain else "continuous",
    )

    while not stop_event.is_set():
        if deadline is not None and time.monotonic() >= deadline:
            logger.info("Email outbox drain reached its runtime limit.")
            break

        try:
            monotonic_now = time.monotonic()
            if not drain and monotonic_now >= next_heartbeat:
                health = await get_email_outbox_health(settings=settings)
                logger.info(
                    "Email outbox heartbeat: worker_id=%s pending=%s "
                    "available_pending=%s processing=%s "
                    "oldest_available_pending_age_seconds=%s "
                    "oldest_processing_age_seconds=%s",
                    worker_id,
                    health["pending_count"],
                    health["available_pending_count"],
                    health["processing_count"],
                    health["oldest_available_pending_age_seconds"],
                    health["oldest_processing_age_seconds"],
                )
                next_heartbeat = monotonic_now + settings.email_outbox_heartbeat_interval_seconds

            recovered = await recover_expired_email_leases(settings=settings)
            if recovered:
                logger.warning("Recovered %s expired email leases.", recovered)

            if stop_event.is_set():
                break

            messages = await claim_email_batch(worker_id, settings=settings)
            if messages:
                logger.info("Claimed %s email outbox message(s).", len(messages))
                # Do not cancel in-flight deliveries on a graceful stop. Each
                # delivery has its own timeout and renews its database lease.
                await asyncio.gather(
                    *(
                        process_email_message(
                            message.id,
                            worker_id,
                            sender,
                            settings=settings,
                        )
                        for message in messages
                    )
                )
                continue

            if drain:
                logger.info("Email outbox drain complete: no currently available messages.")
                break

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.email_outbox_poll_interval_seconds,
                )
            except TimeoutError:
                pass

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Email outbox worker iteration failed.")
            if drain:
                raise
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.email_outbox_poll_interval_seconds,
                )
            except TimeoutError:
                pass

    logger.info("Email outbox worker stopped: %s", worker_id)


async def run_worker_with_cleanup(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 240,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the worker and always release its cached database resources."""
    try:
        await run_email_outbox_worker(
            drain=drain,
            max_runtime_seconds=max_runtime_seconds,
            stop_event=stop_event,
        )
    finally:
        await dispose_engine()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aurora Studio email outbox worker.")
    parser.add_argument(
        "--drain",
        action="store_true",
        help="Process currently available messages and exit instead of polling forever.",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=240,
        help="Maximum drain runtime before stopping new claims (default: 240).",
    )
    return parser.parse_args()


def main() -> None:
    """Run the worker with graceful SIGTERM/SIGINT handling."""
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
                # Windows event loops may not implement add_signal_handler.
                pass

        await run_worker_with_cleanup(
            drain=args.drain,
            max_runtime_seconds=args.max_runtime_seconds,
            stop_event=stop_event,
        )

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Email outbox worker interrupted.")


if __name__ == "__main__":
    main()
