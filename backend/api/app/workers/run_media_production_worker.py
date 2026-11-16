"""Long-running worker for narration, visual generation, rendering and media QA."""

import argparse
import asyncio
import logging
import shutil
import signal
import time
from datetime import timedelta
from uuid import uuid4

from app.core.config import get_settings
from app.database.engine import dispose_engine, get_session_factory
from app.media.comfyui import ComfyUIVisualProvider
from app.media.ffmpeg import FFmpegVideoRenderer
from app.media.html_slides import ChromiumHTMLSlideVisualProvider
from app.media.s3_storage import S3CompatibleMediaStorage
from app.media.storage import LocalMediaStorage
from app.media.tts_registry import TTSProviderRegistry
from app.media.visuals import LocalAbstractVisualProvider, WikimediaFallbackVisualProvider
from app.media.wikimedia import WikimediaCommonsVisualProvider
from app.services.media_artifacts import cleanup_orphaned_media_objects
from app.services.production import recover_expired_production_stage_leases
from app.workers.media_stage_handlers import run_media_stage_once

logger = logging.getLogger(__name__)


async def run_media_production_worker(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 3600,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Poll durable production stages; PostgreSQL remains the queue and lease authority."""
    if max_runtime_seconds <= 0:
        raise ValueError("Maximum worker runtime must be positive.")

    settings = get_settings()
    if not settings.media_worker_enabled:
        logger.info("Media production worker is disabled; no stages were claimed.")
        return

    # Fail before claiming stages if the external media executables are unavailable.
    ffmpeg_executable = shutil.which(settings.media_ffmpeg_path)
    ffprobe_executable = shutil.which(settings.media_ffprobe_path)
    if ffmpeg_executable is None or ffprobe_executable is None:
        raise RuntimeError(
            "Configured FFmpeg and ffprobe executables must be installed and executable."
        )

    # Settings validation guarantees all required provider and encoder configuration.
    session_factory = get_session_factory()
    storage = None
    tts_provider = None
    visual_provider = None
    try:
        if settings.media_storage_backend == "s3":
            storage = S3CompatibleMediaStorage(
                endpoint_url=settings.media_s3_endpoint_url or "",
                bucket=settings.media_s3_bucket or "",
                region=settings.media_s3_region,
                access_key_id=(
                    settings.media_s3_access_key_id.get_secret_value()
                    if settings.media_s3_access_key_id is not None
                    else ""
                ),
                secret_access_key=(
                    settings.media_s3_secret_access_key.get_secret_value()
                    if settings.media_s3_secret_access_key is not None
                    else ""
                ),
                session_token=(
                    settings.media_s3_session_token.get_secret_value()
                    if settings.media_s3_session_token is not None
                    else None
                ),
                max_object_bytes=settings.media_storage_max_object_bytes,
                timeout_seconds=settings.media_s3_timeout_seconds,
                proxy_url=settings.media_s3_proxy_url,
            )
        else:
            storage = LocalMediaStorage(
                settings.media_storage_root,
                max_object_bytes=settings.media_storage_max_object_bytes,
            )
        if settings.media_visual_provider == "comfyui":
            if settings.media_comfyui_workflow_path is None:
                raise RuntimeError("MEDIA_COMFYUI_WORKFLOW_PATH is required for ComfyUI visuals.")
            visual_provider = ComfyUIVisualProvider(
                base_url=settings.media_comfyui_base_url or "",
                workflow_path=settings.media_comfyui_workflow_path,
                output_node_id=settings.media_comfyui_output_node_id or "",
                timeout_seconds=settings.media_comfyui_timeout_seconds,
                max_image_bytes=settings.media_storage_max_object_bytes,
                negative_prompt=settings.media_comfyui_negative_prompt,
                style_prompt=settings.media_comfyui_style_prompt,
                api_key=(
                    settings.media_comfyui_api_key.get_secret_value()
                    if settings.media_comfyui_api_key is not None
                    else None
                ),
                proxy_url=settings.media_visual_proxy_url,
            )
        elif settings.media_visual_provider == "wikimedia_commons":
            commons_provider = WikimediaCommonsVisualProvider(
                user_agent=settings.media_wikimedia_user_agent or "",
                timeout_seconds=settings.media_wikimedia_timeout_seconds,
                max_image_bytes=settings.media_wikimedia_max_image_bytes,
                max_api_response_bytes=settings.media_wikimedia_max_api_response_bytes,
                proxy_url=settings.media_visual_proxy_url,
            )
            visual_provider = WikimediaFallbackVisualProvider(
                commons_provider,
                LocalAbstractVisualProvider(width=720, height=1280),
            )
        else:
            visual_provider = LocalAbstractVisualProvider()
        if settings.media_visual_composer == "chromium_html":
            chromium_executable = shutil.which(settings.media_chromium_path)
            if chromium_executable is None:
                raise RuntimeError("Chromium is required when MEDIA_VISUAL_COMPOSER=chromium_html.")
            visual_provider = ChromiumHTMLSlideVisualProvider(
                visual_provider,
                chromium_path=chromium_executable,
                width=720,
                height=1280,
            )
        tts_registry = TTSProviderRegistry(settings)
        await tts_registry.validate_default()
        tts_provider = tts_registry
        renderer = FFmpegVideoRenderer(
            ffmpeg_path=ffmpeg_executable,
            ffprobe_path=ffprobe_executable,
            video_encoder=settings.media_video_encoder or "",
            audio_encoder=settings.media_audio_encoder,
            motion_style=settings.media_motion_style,
            video_preset=settings.media_video_preset,
            video_crf=settings.media_video_crf,
            max_output_bytes=settings.media_storage_max_object_bytes,
        )

    except Exception:
        try:
            if tts_provider is not None:
                await tts_provider.aclose()
        finally:
            try:
                close_visual = getattr(visual_provider, "aclose", None)
                if close_visual is not None:
                    await close_visual()
            finally:
                if storage is not None:
                    await storage.aclose()
        raise

    assert storage is not None and tts_provider is not None and visual_provider is not None

    stop_event = stop_event or asyncio.Event()
    deadline = time.monotonic() + max_runtime_seconds if drain else None
    last_orphan_cleanup = 0.0
    worker_id = f"media-worker-{uuid4()}"
    logger.info(
        "Media worker started: worker_id=%s mode=%s",
        worker_id,
        "drain" if drain else "continuous",
    )

    try:
        while not stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                logger.info("Media worker drain reached its runtime limit.")
                break
            try:
                recovered = await recover_expired_production_stage_leases(
                    max_attempts=settings.media_worker_max_attempts,
                    session_factory=session_factory,
                )
                if recovered:
                    logger.warning("Media worker recovered %s expired stage lease(s).", recovered)

                now = time.monotonic()
                if now - last_orphan_cleanup >= 3600:
                    try:
                        removed = await cleanup_orphaned_media_objects(
                            session_factory=session_factory,
                            storage=storage,
                            grace_period=timedelta(hours=24),
                            limit=500,
                        )
                        if removed:
                            logger.warning("Media worker removed %s orphaned object(s).", removed)
                        last_orphan_cleanup = now
                    except Exception:
                        logger.exception(
                            "Media object reconciliation failed; production continues."
                        )

                if stop_event.is_set():
                    break

                processed = await run_media_stage_once(
                    worker_id,
                    tts_provider=tts_provider,
                    tts_voice=settings.media_tts_voice or tts_registry.environment_default.voice_id,
                    tts_speed=settings.media_tts_speed,
                    tts_audio_format=settings.media_tts_format,
                    tts_rights_basis=settings.media_tts_rights_basis or "",
                    tts_license_id=settings.media_tts_license_id,
                    tts_license_uri=settings.media_tts_license_uri,
                    tts_source_uri=settings.media_tts_source_uri,
                    tts_attribution=settings.media_tts_attribution,
                    visual_provider=visual_provider,
                    renderer=renderer,
                    storage=storage,
                    session_factory=session_factory,
                    lease_seconds=settings.media_worker_lease_seconds,
                    max_attempts=settings.media_worker_max_attempts,
                )
                if processed:
                    continue
                if drain:
                    logger.info("Media worker drain complete: no runnable media stages.")
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Media worker iteration failed; claimed-stage errors are persisted "
                    "with bounded backoff, and lease recovery handles interruptions."
                )
                if drain:
                    raise

            if not drain:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.media_worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
    finally:
        try:
            await tts_provider.aclose()
        finally:
            try:
                close_visual = getattr(visual_provider, "aclose", None)
                if close_visual is not None:
                    await close_visual()
            finally:
                await storage.aclose()
                logger.info("Media worker stopped: worker_id=%s", worker_id)


async def run_worker_with_cleanup(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 3600,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Dispose database resources even when shutdown or execution fails."""
    try:
        await run_media_production_worker(
            drain=drain,
            max_runtime_seconds=max_runtime_seconds,
            stop_event=stop_event,
        )
    finally:
        await dispose_engine()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aurora Studio media production worker.")
    parser.add_argument(
        "--drain",
        action="store_true",
        help="Process runnable media stages and exit.",
    )
    parser.add_argument("--max-runtime-seconds", type=int, default=3600)
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
        logger.info("Media worker interrupted.")


if __name__ == "__main__":
    main()
