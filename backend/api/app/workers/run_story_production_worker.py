"""Long-running worker for durable story-generation and claim-verification stages."""

import argparse
import asyncio
import logging
import signal
import time
from uuid import uuid4

from app.core.config import get_settings
from app.database.engine import dispose_engine, get_session_factory
from app.llm.providers.anthropic_messages import AnthropicMessagesLLMProvider
from app.llm.providers.ollama_native import OllamaNativeLLMProvider
from app.llm.providers.openai_compatible import OpenAICompatibleLLMProvider
from app.services.production import recover_expired_production_stage_leases
from app.workers.story_stage_handlers import run_story_stage_once

logger = logging.getLogger(__name__)


async def run_story_production_worker(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 240,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Poll durable stage state; PostgreSQL remains the queue and lease authority."""
    if max_runtime_seconds <= 0:
        raise ValueError("Maximum worker runtime must be positive.")

    settings = get_settings()
    if not settings.story_worker_enabled:
        logger.info("Story production worker is disabled; no stages were claimed.")
        return

    if not settings.story_worker_llm_base_url or not settings.story_worker_model:
        raise RuntimeError(
            "Story worker is enabled but its LLM base URL or model is not configured."
        )

    session_factory = get_session_factory()
    if settings.story_worker_provider == "anthropic":
        if settings.story_worker_llm_api_key is None:
            raise RuntimeError("Anthropic API key is missing.")
        provider = AnthropicMessagesLLMProvider(
            base_url=settings.story_worker_llm_base_url,
            api_key=settings.story_worker_llm_api_key,
            timeout_seconds=settings.story_worker_timeout_seconds,
        )
    elif settings.story_worker_provider == "ollama":
        provider = OllamaNativeLLMProvider(
            base_url=settings.story_worker_llm_base_url,
            api_key=settings.story_worker_llm_api_key,
            timeout_seconds=settings.story_worker_timeout_seconds,
        )
    else:
        provider = OpenAICompatibleLLMProvider(
            base_url=settings.story_worker_llm_base_url,
            api_key=settings.story_worker_llm_api_key,
            provider_id=(
                "openai-compatible"
                if settings.story_worker_provider == "openai_compatible"
                else settings.story_worker_provider.replace("_", "-")
            ),
            reasoning_effort=settings.story_worker_reasoning_effort,
            timeout_seconds=settings.story_worker_timeout_seconds,
        )
    stop_event = stop_event or asyncio.Event()
    deadline = time.monotonic() + max_runtime_seconds if drain else None
    worker_id = f"story-worker-{uuid4()}"
    logger.info(
        "Story worker started: worker_id=%s mode=%s",
        worker_id,
        "drain" if drain else "continuous",
    )

    try:
        while not stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                logger.info("Story worker drain reached its runtime limit.")
                break
            try:
                recovered = await recover_expired_production_stage_leases(
                    max_attempts=settings.story_worker_max_attempts,
                    session_factory=session_factory,
                )
                if recovered:
                    logger.warning("Story worker recovered %s expired stage lease(s).", recovered)

                if stop_event.is_set():
                    break

                processed = await run_story_stage_once(
                    worker_id,
                    provider=provider,
                    model=settings.story_worker_model,
                    verification_model=(
                        settings.story_worker_verification_model or settings.story_worker_model
                    ),
                    session_factory=session_factory,
                    lease_seconds=settings.story_worker_lease_seconds,
                    max_attempts=settings.story_worker_max_attempts,
                )
                if processed:
                    continue
                if drain:
                    logger.info("Story worker drain complete: no runnable story stages.")
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Story worker iteration failed; the durable lease will govern retry."
                )
                if drain:
                    raise

            if not drain:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.story_worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
    finally:
        await provider.aclose()
        logger.info("Story worker stopped: worker_id=%s", worker_id)


async def run_worker_with_cleanup(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 240,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Dispose database resources even when shutdown or execution fails."""
    try:
        await run_story_production_worker(
            drain=drain,
            max_runtime_seconds=max_runtime_seconds,
            stop_event=stop_event,
        )
    finally:
        await dispose_engine()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aurora Studio story production worker.")
    parser.add_argument(
        "--drain",
        action="store_true",
        help="Process runnable story stages and exit.",
    )
    parser.add_argument("--max-runtime-seconds", type=int, default=240)
    return parser.parse_args()


def main() -> None:
    """Run with SIGINT/SIGTERM handling and bounded drain support."""
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
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Story worker interrupted.")


if __name__ == "__main__":
    main()
