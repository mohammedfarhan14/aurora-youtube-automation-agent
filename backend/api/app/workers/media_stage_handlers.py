"""Durable handlers for media direction, narration, visuals, rendering and QA."""

import asyncio
import hashlib
import re
from dataclasses import asdict, is_dataclass, replace
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.media.providers import (
    AudioAsset,
    BackgroundMusicProvider,
    RenderRequest,
    TTSProvider,
    TTSWordTiming,
    VideoRenderer,
    VisualAsset,
    VisualProvider,
    WhisperXAlignmentProvider,
)
from app.media.storage import MediaStorage
from app.media.thumbnail import generate_thumbnail
from app.media.tts_registry import TTSProviderRegistry, resolve_tts_selection
from app.models.media_artifact import MediaArtifact
from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage
from app.services.media_artifacts import (
    MediaArtifactResult,
    MediaStageLeaseError,
    store_media_artifact,
)
from app.services.production import (
    claim_production_stage,
    complete_production_stage,
    renew_production_stage_lease,
)
from app.services.story_artifacts import load_latest_story_artifact

MEDIA_STAGE_KEYS = (
    "ai_direction",
    "narration",
    "visual_generation",
    "rendering",
    "quality_check",
)


class MediaQualityGateError(RuntimeError):
    """A rendered video failed a deterministic technical quality gate."""


async def _maintain_stage_lease(
    stage_id: UUID,
    lease_token: UUID,
    *,
    lease_seconds: int,
    stop: asyncio.Event,
    lost: asyncio.Event,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    interval = max(0.1, lease_seconds / 3)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            renewed = await renew_production_stage_lease(
                stage_id,
                lease_token,
                lease_seconds=lease_seconds,
                session_factory=session_factory,
            )
            if not renewed:
                async with session_factory() as session:
                    stage = await session.get(ProductionStage, stage_id)
                    if stage is not None and stage.status == "completed":
                        return
                lost.set()
                return


def _vtt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _escape_vtt_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _caption_word_key(value: str) -> str:
    return re.sub(r"^\W+|\W+$", "", value, flags=re.UNICODE).casefold()


def _captions_vtt(
    scenes: list[dict],
    *,
    word_timings: tuple[TTSWordTiming, ...] = (),
    audio_duration_ms: int | None = None,
    target_duration_ms: int | None = None,
) -> str:
    """Build readable phrase-level WebVTT cues, preserving provider alignment.

    Scene durations describe visual cuts, not the speech clock. FFmpeg renders
    narration to the target duration with an ``atempo`` filter, so provider
    timings (which are measured on the source WAV) must be mapped onto that
    rendered clock. Otherwise a 24 second narration stretched to a 30 second
    short makes every caption progressively late. Providers without word
    timings are distributed directly over the rendered target timeline.
    """
    words_by_scene = []
    scene_plan_duration_ms = 0
    for scene in scenes:
        duration_ms = int(scene["duration_seconds"]) * 1_000
        words = str(scene["narration_segment"]).split()
        if duration_ms <= 0 or not words:
            raise ValueError("Caption scenes require positive duration and narration text.")
        words_by_scene.append((duration_ms, words))
        scene_plan_duration_ms += duration_ms

    words = [word for _, scene_words in words_by_scene for word in scene_words]
    if not words:
        raise ValueError("Word-level captions require narration text.")
    target_timeline_ms = int(target_duration_ms or scene_plan_duration_ms)
    if target_timeline_ms <= 0:
        raise ValueError("Caption target duration must be positive.")
    source_audio_duration_ms = (
        max(1, int(audio_duration_ms)) if audio_duration_ms is not None else target_timeline_ms
    )

    cues = ["WEBVTT", ""]
    if word_timings:
        if len(word_timings) != len(words):
            raise ValueError("Word alignment count does not match narration token count.")
        previous_end_ms = 0
        aligned: list[tuple[str, int, int]] = []
        for index, (word, timing) in enumerate(zip(words, word_timings, strict=True)):
            aligned_word = str(timing.text).split()
            if len(aligned_word) != 1 or _caption_word_key(aligned_word[0]) != _caption_word_key(
                word
            ):
                raise ValueError("Word alignment tokens do not match narration text.")
            source_start_ms = int(timing.start_ms)
            source_end_ms = int(timing.end_ms)
            if (
                source_start_ms < previous_end_ms
                or source_end_ms <= source_start_ms
                or source_end_ms > source_audio_duration_ms
            ):
                raise ValueError("Word alignment contains invalid or overlapping timestamps.")
            # The renderer applies atempo=source_duration/target_duration.
            # Mapping the source timing by the inverse factor keeps captions
            # locked to the narration after FFmpeg has stretched/compressed it.
            start_ms = min(
                target_timeline_ms - 1,
                round(source_start_ms * target_timeline_ms / source_audio_duration_ms),
            )
            end_ms = round(source_end_ms * target_timeline_ms / source_audio_duration_ms)
            end_ms = min(target_timeline_ms, max(start_ms + 1, end_ms))
            if audio_duration_ms is None and index == len(word_timings) - 1:
                end_ms = target_timeline_ms
            aligned.append((word, start_ms, end_ms))
            previous_end_ms = source_end_ms
        groups: list[list[tuple[str, int, int]]] = []
        for item in aligned:
            if not groups or len(groups[-1]) >= 5 or re.search(r"[.!?]$", groups[-1][-1][0]):
                groups.append([])
            groups[-1].append(item)
        for group in groups:
            cues.extend(
                [
                    f"{_vtt_timestamp(group[0][1])} --> {_vtt_timestamp(group[-1][2])}",
                    _escape_vtt_text(" ".join(item[0] for item in group)),
                    "",
                ]
            )
        last_caption_end_ms = aligned[-1][2]
    else:
        cursor_ms = 0
        for scene_index, (duration_ms, scene_words) in enumerate(words_by_scene):
            scaled_duration_ms = round(
                target_timeline_ms * duration_ms / scene_plan_duration_ms
            )
            if scene_index == len(words_by_scene) - 1:
                scaled_duration_ms = target_timeline_ms - cursor_ms
            if scaled_duration_ms / len(scene_words) < 150:
                raise ValueError("Narration is too dense for readable phrase-level captions.")
            weights = [max(len(_caption_word_key(word)), 1) for word in scene_words]
            total_weight = sum(weights)
            groups: list[list[tuple[str, int]]] = []
            for word, weight in zip(scene_words, weights, strict=True):
                if not groups or len(groups[-1]) >= 5 or re.search(r"[.!?]$", groups[-1][-1][0]):
                    groups.append([])
                groups[-1].append((word, weight))
            cumulative_weight = 0
            for group_index, group in enumerate(groups):
                start_ms = cursor_ms + round(
                    scaled_duration_ms * cumulative_weight / total_weight
                )
                group_weight = sum(weight for _, weight in group)
                cumulative_weight += group_weight
                end_ms = (
                    cursor_ms + scaled_duration_ms
                    if group_index == len(groups) - 1
                    else cursor_ms
                    + round(scaled_duration_ms * cumulative_weight / total_weight)
                )
                if end_ms <= start_ms:
                    raise ValueError("Narration is too dense for readable phrase-level captions.")
                cues.extend(
                    [
                        f"{_vtt_timestamp(start_ms)} --> {_vtt_timestamp(end_ms)}",
                        _escape_vtt_text(" ".join(word for word, _ in group)),
                        "",
                    ]
                )
            cursor_ms += scaled_duration_ms
        last_caption_end_ms = cursor_ms
    if last_caption_end_ms < target_timeline_ms:
        # A zero-width space is accepted by WebVTT but renders invisibly. It
        # keeps the sidecar timeline aligned with padded video duration.
        cues.extend(
            [
                f"{_vtt_timestamp(last_caption_end_ms)} --> {_vtt_timestamp(target_timeline_ms)}",
                "\u200b",
                "",
            ]
        )
    return "\n".join(cues)


def _visual_settings_snapshot(profile: object) -> dict[str, str]:
    values = profile if isinstance(profile, dict) else {}
    style = values.get("visual_style")
    lighting = values.get("visual_lighting")
    motion = values.get("visual_motion")
    aspect_ratio = values.get("video_aspect_ratio")
    settings = {
        "style": style[:500] if isinstance(style, str) else "",
        "lighting": (
            lighting
            if isinstance(lighting, str)
            and lighting in {"natural", "cinematic", "studio", "dramatic"}
            else "cinematic"
        ),
        "motion": (
            motion
            if isinstance(motion, str) and motion in {"static", "gentle_drift"}
            else "gentle_drift"
        ),
        "aspect_ratio": (
            aspect_ratio
            if isinstance(aspect_ratio, str) and aspect_ratio in {"9:16", "16:9", "1:1"}
            else "9:16"
        ),
    }
    # Older profile snapshots do not carry caption preferences. Do not change
    # their immutable direction manifest shape; rendering already applies the
    # same safe defaults. New profiles include these keys and therefore carry
    # the user-selected caption controls through the pipeline.
    if any(key in values for key in ("caption_style", "caption_position", "caption_size")):
        settings.update(
            {
                "caption_style": (
                    values.get("caption_style")
                    if values.get("caption_style") in {"classic", "bold_outline"}
                    else "bold_outline"
                ),
                "caption_position": (
                    values.get("caption_position")
                    if values.get("caption_position") in {"lower_safe", "center"}
                    else "lower_safe"
                ),
                "caption_size": (
                    values.get("caption_size")
                    if values.get("caption_size") in {"small", "medium", "large"}
                    else "medium"
                ),
            }
        )
    return settings


async def _load_run_and_story(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    organization_id: UUID,
    production_run_id: UUID,
):
    async with session_factory() as session:
        run = await session.scalar(
            select(ProductionRun).where(
                ProductionRun.id == production_run_id,
                ProductionRun.organization_id == organization_id,
            )
        )
        if run is None:
            raise LookupError("Production run not found in this organization.")
        story = await load_latest_story_artifact(
            session,
            organization_id=organization_id,
            production_run_id=production_run_id,
            artifact_type="story_verified",
        )
        return run, story


def _source_rights_snapshot(story) -> list[dict]:
    """Serialize application-owned source rights and identity metadata for media stages."""
    snapshots = []
    for source in getattr(story, "source_references", ()):
        rights = source.rights_metadata
        snapshots.append(
            {
                "source_id": str(source.source_id),
                "canonical_url": source.canonical_url,
                "title": source.title,
                "publisher": source.publisher,
                "status": rights.status.value,
                "scope": rights.scope.value,
                "license_ids": list(rights.license_ids),
                "license_urls": list(rights.license_urls),
                "rights_holder": rights.rights_holder,
                "creators": list(rights.creators),
                "source_revision_url": rights.source_revision_url,
                "attribution_text": rights.attribution_text,
                "reuse_restrictions": [
                    restriction.value for restriction in rights.reuse_restrictions
                ],
            }
        )
    return snapshots


def _source_rights_attribution_credits(source_rights: object) -> list[dict]:
    """Create deterministic source credits from a bounded rights snapshot."""
    if not isinstance(source_rights, list):
        return []
    credits = []
    for item in source_rights:
        if not isinstance(item, dict):
            continue
        credits.append(
            {
                "source_id": item.get("source_id"),
                "source_uri": item.get("canonical_url"),
                "title": item.get("title"),
                "publisher": item.get("publisher"),
                "rights_status": item.get("status"),
                "rights_scope": item.get("scope"),
                "license_ids": item.get("license_ids", []),
                "license_urls": item.get("license_urls", []),
                "rights_holder": item.get("rights_holder"),
                "creators": item.get("creators", []),
                "source_revision_url": item.get("source_revision_url"),
                "reuse_restrictions": item.get("reuse_restrictions", []),
                "modification_note": (
                    "Summarized in original short-form narration."
                    if "indicate_modifications" in item.get("reuse_restrictions", [])
                    else None
                ),
                "attribution": item.get("attribution_text"),
            }
        )
    return credits


def _source_rights_clearance_flags(source_rights: object) -> list[dict]:
    """Fail closed unless every research source has cleared, compatible rights."""
    if not isinstance(source_rights, list) or not source_rights:
        return [{"source_id": None, "reason": "source_rights_manifest_missing"}]

    flags = []
    blocked_restrictions = {
        "share_alike",
        "non_commercial",
        "no_derivatives",
        "no_verbatim_reuse_without_license",
        "review_page_history",
        "unknown",
    }
    for item in source_rights:
        if not isinstance(item, dict):
            flags.append({"source_id": None, "reason": "source_rights_entry_invalid"})
            continue
        source_id = item.get("source_id")
        status = item.get("status")
        if status != "cleared":
            flags.append(
                {
                    "source_id": source_id,
                    "reason": "source_rights_not_cleared",
                    "rights_status": status or "unknown",
                }
            )
        if not item.get("canonical_url") or not item.get("title"):
            flags.append({"source_id": source_id, "reason": "source_identity_metadata_missing"})
        if item.get("scope") in {None, "unknown"}:
            flags.append({"source_id": source_id, "reason": "source_rights_scope_missing"})
        license_ids = item.get("license_ids")
        license_urls = item.get("license_urls")
        if (
            not isinstance(license_ids, list)
            or not license_ids
            or not isinstance(license_urls, list)
            or len(license_ids) != len(license_urls)
        ):
            flags.append({"source_id": source_id, "reason": "source_license_metadata_missing"})
        if not item.get("attribution_text"):
            flags.append({"source_id": source_id, "reason": "source_attribution_missing"})
        restrictions = item.get("reuse_restrictions")
        if not isinstance(restrictions, list):
            restrictions = ["unknown"]
        incompatible = sorted(set(restrictions).intersection(blocked_restrictions))
        if incompatible:
            flags.append(
                {
                    "source_id": source_id,
                    "reason": "source_reuse_restriction_unmet",
                    "reuse_restrictions": incompatible,
                }
            )
    return flags


async def _load_stage_manifest(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    production_run_id: UUID,
    stage_key: str,
) -> dict:
    async with session_factory() as session:
        stage = await session.scalar(
            select(ProductionStage).where(
                ProductionStage.production_run_id == production_run_id,
                ProductionStage.stage_key == stage_key,
                ProductionStage.status == "completed",
            )
        )
        if stage is None or not isinstance(stage.output_manifest, dict):
            raise LookupError(f"Completed {stage_key} stage manifest is missing.")
        return dict(stage.output_manifest)


async def _load_media_artifact(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    organization_id: UUID,
    production_run_id: UUID,
    artifact_id: UUID,
) -> MediaArtifactResult:
    async with session_factory() as session:
        record = await session.scalar(
            select(MediaArtifact).where(
                MediaArtifact.id == artifact_id,
                MediaArtifact.organization_id == organization_id,
                MediaArtifact.production_run_id == production_run_id,
            )
        )
        if record is None:
            raise LookupError("Media artifact does not belong to this production run.")
        return MediaArtifactResult(
            id=record.id,
            organization_id=record.organization_id,
            production_run_id=record.production_run_id,
            production_stage_id=record.production_stage_id,
            artifact_kind=record.artifact_kind,
            storage_backend=record.storage_backend,
            storage_key=record.storage_key,
            mime_type=record.mime_type,
            sha256=record.sha256,
            byte_size=record.byte_size,
            duration_ms=record.duration_ms,
            width=record.width,
            height=record.height,
            metadata_json=dict(record.metadata_json),
        )


async def _read_artifact(storage: MediaStorage, artifact: MediaArtifactResult) -> bytes:
    content = await storage.get(artifact.storage_key)
    if len(content) != artifact.byte_size or hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise RuntimeError("Stored media artifact failed its integrity check.")
    return content


async def execute_ai_direction_stage(
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Create a deterministic scene/caption plan and preserve factuality flags."""

    run, story = await _load_run_and_story(
        session_factory,
        organization_id=organization_id,
        production_run_id=production_run_id,
    )
    scenes = [
        {
            "scene_index": index,
            # Keep the story topic beside every shot direction. SD 1.5 has a
            # short text-conditioning window; without this anchor it can
            # satisfy generic style words with a scenic stock image unrelated
            # to the episode subject.
            "visual_direction": (
                f"Primary topic: {story.title}. {scene.visual_direction}"
            )[:2000],
            "narration_segment": scene.narration_segment,
            "duration_seconds": scene.duration_seconds,
        }
        for index, scene in enumerate(story.scenes)
    ]
    claim_flags = [
        {
            "claim": claim.claim,
            "verification_status": claim.verification_status.value,
        }
        for claim in story.factual_claims
        if claim.verification_status.value != "supported"
    ]
    source_rights = _source_rights_snapshot(story)
    source_rights_flags = _source_rights_clearance_flags(source_rights)
    profile = getattr(run, "channel_profile_snapshot", None)
    manifest = {
        "schema_version": "1.2.0",
        "title": story.title,
        "hook": story.hook,
        "narration": story.narration,
        "target_duration_seconds": story.target_duration_seconds,
        "scene_durations_seconds": [scene["duration_seconds"] for scene in scenes],
        "scenes": scenes,
        "visual_settings": _visual_settings_snapshot(profile),
        "captions_vtt": _captions_vtt(scenes),
        "claim_flags": claim_flags,
        "source_rights": source_rights,
        "source_rights_flags": source_rights_flags,
        "publication_blocked": bool(claim_flags or source_rights_flags),
    }
    completed = await complete_production_stage(
        production_stage_id,
        lease_token,
        output_manifest=manifest,
        provider_id="application",
        model_id="deterministic-scene-planner-v1",
        prompt_version="media-direction.v3",
        session_factory=session_factory,
    )
    if not completed:
        raise MediaStageLeaseError("Media direction stage lease was lost before completion.")


async def execute_narration_stage(
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    attempt_count: int,
    provider: TTSProvider | TTSProviderRegistry,
    voice_id: str,
    rights_basis: str,
    license_id: str | None,
    license_uri: str | None,
    source_uri: str | None,
    attribution: str | None,
    storage: MediaStorage,
    session_factory: async_sessionmaker[AsyncSession],
    speed: float = 1.0,
    audio_format: str = "wav",
) -> None:
    run, story = await _load_run_and_story(
        session_factory,
        organization_id=organization_id,
        production_run_id=production_run_id,
    )
    profile = run.channel_profile_snapshot or {}
    language = str(profile.get("language") or "en")
    active_provider = provider
    selected_voice = voice_id
    selected_speed = speed
    selected_format = audio_format
    close_active_provider = False
    if isinstance(provider, TTSProviderRegistry):
        selection = resolve_tts_selection(
            environment_default=provider.environment_default,
            channel_profile=profile,
            run_override=profile.get("_tts_run_override"),
        )
        active_provider = provider.create(selection.provider_id)
        selected_voice = selection.voice_id
        selected_speed = selection.speed
        selected_format = selection.audio_format
        close_active_provider = True

    try:
        health_check = getattr(active_provider, "health_check", None)
        if health_check is not None and not await health_check():
            raise RuntimeError("The selected TTS provider failed its health check.")
        audio = await active_provider.synthesize(
            text=story.narration,
            voice_id=selected_voice,
            language=language,
            speed=selected_speed,
            audio_format=selected_format,
        )
        manifest = getattr(active_provider, "capability_manifest", None)
        manifest_data = (
            asdict(manifest) if manifest is not None and is_dataclass(manifest) else None
        )
    finally:
        if close_active_provider:
            await active_provider.aclose()
    artifact_rights_basis = rights_basis
    artifact_license_id = license_id
    artifact_license_uri = license_uri
    artifact_source_uri = source_uri
    artifact_attribution = attribution
    if manifest_data is not None:
        if manifest_data["provider_id"] == "mock":
            artifact_rights_basis = "generated_in_application"
            artifact_license_id = None
            artifact_license_uri = None
            artifact_source_uri = None
            artifact_attribution = manifest_data.get("attribution")
        else:
            artifact_license_id = manifest_data.get("voice_license") or license_id
            artifact_license_uri = manifest_data.get("license_uri") or license_uri
            artifact_attribution = manifest_data.get("attribution") or attribution

    artifact = await store_media_artifact(
        session_factory=session_factory,
        storage=storage,
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        attempt_count=attempt_count,
        artifact_slot="narration-main",
        artifact_kind="audio",
        extension="wav",
        mime_type=audio.mime_type,
        content=audio.content,
        duration_ms=audio.duration_ms,
        metadata={
            "provider_id": audio.provider_id,
            "model_id": audio.model_id,
            "voice_id": audio.voice_id,
            "request_id": audio.request_id,
            "sample_rate_hz": audio.sample_rate_hz,
            "channels": audio.channels,
            "language": language,
            "rights_basis": artifact_rights_basis,
            "license_id": artifact_license_id,
            "license_uri": artifact_license_uri,
            "source_uri": artifact_source_uri,
            "attribution": artifact_attribution,
            "capability_manifest": manifest_data,
            "word_timings": [
                {"start_ms": timing.start_ms, "end_ms": timing.end_ms, "text": timing.text}
                for timing in audio.word_timings
            ],
        },
    )
    completed = await complete_production_stage(
        production_stage_id,
        lease_token,
        output_manifest={
            "audio_artifact_id": str(artifact.id),
            "duration_ms": artifact.duration_ms,
            "provider_id": audio.provider_id,
            "model_id": audio.model_id,
            "voice_id": audio.voice_id,
        },
        provider_id=audio.provider_id,
        model_id=audio.model_id,
        prompt_version="tts-request.v1",
        request_id=audio.request_id,
        session_factory=session_factory,
    )
    if not completed:
        raise MediaStageLeaseError("Narration stage lease was lost before completion.")


async def execute_visual_generation_stage(
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    attempt_count: int,
    provider: VisualProvider,
    storage: MediaStorage,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    direction = await _load_stage_manifest(
        session_factory,
        production_run_id=production_run_id,
        stage_key="ai_direction",
    )
    visual_settings = direction.get("visual_settings", {})
    visual_style = str(visual_settings.get("style") or "")
    lighting = str(visual_settings.get("lighting") or "cinematic")
    output_scenes = []
    for scene in direction["scenes"]:
        visual = await provider.generate(
            visual_direction=scene["visual_direction"],
            narration_segment=scene["narration_segment"],
            scene_index=int(scene["scene_index"]),
            visual_style=visual_style,
            lighting=lighting,
        )
        extension = {"image/png": "png", "image/jpeg": "jpg"}.get(visual.mime_type)
        if extension is None:
            raise ValueError("Visual provider returned an unsupported image MIME type.")
        if not isinstance(visual.contains_synthetic_media, bool):
            raise ValueError("Visual provider must declare its synthetic-media status.")
        artifact = await store_media_artifact(
            session_factory=session_factory,
            storage=storage,
            organization_id=organization_id,
            production_run_id=production_run_id,
            production_stage_id=production_stage_id,
            lease_token=lease_token,
            attempt_count=attempt_count,
            artifact_slot=f"scene-{int(scene['scene_index']):03d}",
            artifact_kind="image",
            extension=extension,
            mime_type=visual.mime_type,
            content=visual.content,
            width=visual.width,
            height=visual.height,
            metadata={
                "provider_id": visual.provider_id,
                "rights_basis": visual.rights_basis,
                "attribution": visual.attribution,
                "source_uri": visual.source_uri,
                "license_id": visual.license_id,
                "license_uri": visual.license_uri,
                "contains_synthetic_media": visual.contains_synthetic_media,
                "scene_index": int(scene["scene_index"]),
            },
        )
        output_scenes.append(
            {
                "scene_index": int(scene["scene_index"]),
                "duration_seconds": int(scene["duration_seconds"]),
                "artifact_id": str(artifact.id),
                "provider_id": visual.provider_id,
                "rights_basis": visual.rights_basis,
                "license_id": visual.license_id,
                "license_uri": visual.license_uri,
                "contains_synthetic_media": visual.contains_synthetic_media,
            }
        )

    provider_ids = sorted({scene["provider_id"] for scene in output_scenes})
    stage_provider_id = provider_ids[0] if len(provider_ids) == 1 else "mixed_visual_providers"
    completed = await complete_production_stage(
        production_stage_id,
        lease_token,
        output_manifest={"scenes": output_scenes, "provider_ids": provider_ids},
        provider_id=stage_provider_id,
        model_id="visual-provider-v1",
        prompt_version="visual-provider.v1",
        session_factory=session_factory,
    )
    if not completed:
        raise MediaStageLeaseError("Visual generation lease was lost before completion.")


async def execute_rendering_stage(
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    attempt_count: int,
    renderer: VideoRenderer,
    storage: MediaStorage,
    session_factory: async_sessionmaker[AsyncSession],
    background_music_provider: BackgroundMusicProvider | None = None,
    alignment_provider: WhisperXAlignmentProvider | None = None,
) -> None:
    direction = await _load_stage_manifest(
        session_factory,
        production_run_id=production_run_id,
        stage_key="ai_direction",
    )
    visual_settings = direction.get("visual_settings", {})
    dimensions = {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
    }
    width, height = dimensions.get(str(visual_settings.get("aspect_ratio") or "9:16"), (1080, 1920))
    narration_manifest = await _load_stage_manifest(
        session_factory,
        production_run_id=production_run_id,
        stage_key="narration",
    )
    visuals_manifest = await _load_stage_manifest(
        session_factory,
        production_run_id=production_run_id,
        stage_key="visual_generation",
    )
    audio_record = await _load_media_artifact(
        session_factory,
        organization_id=organization_id,
        production_run_id=production_run_id,
        artifact_id=UUID(narration_manifest["audio_artifact_id"]),
    )
    audio_content = await _read_artifact(storage, audio_record)
    audio_metadata = audio_record.metadata_json
    audio = AudioAsset(
        content=audio_content,
        mime_type=audio_record.mime_type,
        duration_ms=audio_record.duration_ms or 0,
        sample_rate_hz=int(audio_metadata["sample_rate_hz"]),
        channels=int(audio_metadata["channels"]),
        provider_id=str(audio_metadata["provider_id"]),
        model_id=str(audio_metadata["model_id"]),
        voice_id=str(audio_metadata["voice_id"]),
        request_id=audio_metadata.get("request_id"),
        word_timings=tuple(
            TTSWordTiming(
                start_ms=int(item["start_ms"]),
                end_ms=int(item["end_ms"]),
                text=str(item["text"]),
            )
            for item in audio_metadata.get("word_timings", [])
            if isinstance(item, dict)
        ),
    )
    if not audio.word_timings and alignment_provider is not None:
        narration_text = " ".join(str(scene["narration_segment"]) for scene in direction["scenes"])
        aligned_timings = await alignment_provider.align(
            audio=audio,
            text=narration_text,
            language=str(audio_metadata.get("language") or "en"),
        )
        audio = replace(audio, word_timings=tuple(aligned_timings))

    captions_vtt = _captions_vtt(
        direction["scenes"],
        word_timings=audio.word_timings,
        audio_duration_ms=audio.duration_ms,
        target_duration_ms=int(direction["target_duration_seconds"]) * 1_000,
    )
    scene_visuals = []
    visual_records = []
    ordered_scenes = sorted(visuals_manifest["scenes"], key=lambda item: item["scene_index"])
    for visual_manifest in ordered_scenes:
        visual_record = await _load_media_artifact(
            session_factory,
            organization_id=organization_id,
            production_run_id=production_run_id,
            artifact_id=UUID(visual_manifest["artifact_id"]),
        )
        visual_content = await _read_artifact(storage, visual_record)
        visual_metadata = visual_record.metadata_json
        visual_records.append(visual_record)
        scene_visuals.append(
            VisualAsset(
                content=visual_content,
                mime_type=visual_record.mime_type,
                width=visual_record.width or 0,
                height=visual_record.height or 0,
                provider_id=str(visual_metadata["provider_id"]),
                rights_basis=str(visual_metadata["rights_basis"]),
                attribution=visual_metadata.get("attribution"),
                source_uri=visual_metadata.get("source_uri"),
                license_id=visual_metadata.get("license_id"),
                license_uri=visual_metadata.get("license_uri"),
                contains_synthetic_media=visual_metadata.get("contains_synthetic_media"),
            )
        )

    background_music_asset = None
    if background_music_provider is not None:
        background_music_asset = await background_music_provider.select_track(
            organization_id=organization_id,
            production_run_id=production_run_id,
        )
    synthetic_values = [
        record.metadata_json.get("contains_synthetic_media") for record in visual_records
    ]
    if not all(isinstance(value, bool) for value in synthetic_values):
        raise ValueError("Visual artifacts must declare synthetic-media status.")
    contains_synthetic_media = any(value is True for value in synthetic_values)
    request = RenderRequest(
        visual_assets=tuple(scene_visuals),
        audio_asset=audio,
        captions_vtt=captions_vtt,
        background_music_asset=background_music_asset,
        scene_durations_seconds=tuple(int(item) for item in direction["scene_durations_seconds"]),
        target_duration_seconds=int(direction["target_duration_seconds"]),
        width=width,
        height=height,
        motion_style=str(visual_settings.get("motion") or "gentle_drift"),
        caption_style=str(visual_settings.get("caption_style") or "bold_outline"),
        caption_position=str(visual_settings.get("caption_position") or "lower_safe"),
        caption_size=str(visual_settings.get("caption_size") or "medium"),
    )
    video = await renderer.render(request)
    background_music_metadata = (video.metadata or {}).get("background_music")
    if not isinstance(background_music_metadata, dict):
        background_music_metadata = None
    video_record = await store_media_artifact(
        session_factory=session_factory,
        storage=storage,
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        attempt_count=attempt_count,
        artifact_slot="render-final",
        artifact_kind="video",
        extension="mp4",
        mime_type=video.mime_type,
        content=video.content,
        duration_ms=video.duration_ms,
        width=video.width,
        height=video.height,
        metadata={
            "provider_id": video.provider_id,
            "video_codec": video.video_codec,
            "audio_codec": video.audio_codec,
            "background_music": background_music_metadata,
            "contains_synthetic_media": contains_synthetic_media,
            "audio_rights": {
                "artifact_id": str(audio_record.id),
                "provider_id": audio_metadata.get("provider_id"),
                "rights_basis": audio_metadata.get("rights_basis"),
                "license_id": audio_metadata.get("license_id"),
                "license_uri": audio_metadata.get("license_uri"),
                "source_uri": audio_metadata.get("source_uri"),
                "attribution": audio_metadata.get("attribution"),
            },
            "source_rights": direction.get("source_rights", []),
            "rights_manifest_version": "1.0.0",
            "attribution_credits": [
                {
                    "artifact_id": str(record.id),
                    "provider_id": record.metadata_json.get("provider_id"),
                    "rights_basis": record.metadata_json.get("rights_basis"),
                    "source_uri": record.metadata_json.get("source_uri"),
                    "license_id": record.metadata_json.get("license_id"),
                    "license_uri": record.metadata_json.get("license_uri"),
                    "attribution": record.metadata_json.get("attribution"),
                }
                for record in visual_records
                if record.metadata_json.get("attribution")
            ]
            + (
                [
                    {
                        "artifact_id": str(audio_record.id),
                        "provider_id": audio_metadata.get("provider_id"),
                        "rights_basis": audio_metadata.get("rights_basis"),
                        "license_id": audio_metadata.get("license_id"),
                        "license_uri": audio_metadata.get("license_uri"),
                        "source_uri": audio_metadata.get("source_uri"),
                        "attribution": audio_metadata.get("attribution"),
                    }
                ]
                if audio_metadata.get("attribution")
                else []
            )
            + (
                [
                    {
                        "artifact_id": None,
                        "provider_id": "background_music",
                        "rights_basis": "licensed",
                        "source_uri": background_music_metadata.get("source_url"),
                        "license_id": background_music_metadata.get("license_id"),
                        "license_uri": background_music_metadata.get("license_url"),
                        "attribution": background_music_metadata.get("attribution_text"),
                    }
                ]
                if background_music_metadata is not None
                else []
            )
            + _source_rights_attribution_credits(direction.get("source_rights", [])),
        },
    )
    captions_record = await store_media_artifact(
        session_factory=session_factory,
        storage=storage,
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        attempt_count=attempt_count,
        artifact_slot="captions-vtt",
        artifact_kind="captions",
        extension="vtt",
        mime_type="text/vtt",
        content=captions_vtt.encode("utf-8"),
        duration_ms=video.duration_ms,
        metadata={
            "format": "webvtt",
            "generated_by": "media-direction.v1",
            "rights_basis": "generated_in_application",
        },
    )
    thumbnail_content = generate_thumbnail(
        title=str(direction.get("title") or "Aurora Studio Short"),
        background_image=scene_visuals[0].content,
    )
    thumbnail_record = await store_media_artifact(
        session_factory=session_factory,
        storage=storage,
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        attempt_count=attempt_count,
        artifact_slot="thumbnail-short",
        artifact_kind="thumbnail",
        extension="jpg",
        mime_type="image/jpeg",
        content=thumbnail_content,
        width=1080,
        height=1920,
        metadata={
            "generated_by": "aurora-thumbnail.v1",
            "source_visual_artifact_id": str(visual_records[0].id),
            "rights_basis": "generated_in_application",
            "contains_synthetic_media": contains_synthetic_media,
        },
    )
    completed = await complete_production_stage(
        production_stage_id,
        lease_token,
        output_manifest={
            "video_artifact_id": str(video_record.id),
            "captions_artifact_id": str(captions_record.id),
            "thumbnail_artifact_id": str(thumbnail_record.id),
            "duration_ms": video.duration_ms,
            "width": video.width,
            "height": video.height,
            "video_codec": video.video_codec,
            "audio_codec": video.audio_codec,
            "contains_synthetic_media": contains_synthetic_media,
        },
        provider_id=video.provider_id,
        model_id=video.video_codec,
        prompt_version="ffmpeg-render.v1",
        session_factory=session_factory,
    )
    if not completed:
        raise MediaStageLeaseError("Rendering stage lease was lost before completion.")


def _is_https_provenance_uri(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and not parsed.query
        and not parsed.fragment
    )


def _rights_clearance_flag(
    *,
    artifact_id: UUID,
    artifact_kind: str,
    metadata: dict,
) -> dict | None:
    basis = metadata.get("rights_basis")
    license_id = metadata.get("license_id")
    license_uri = metadata.get("license_uri")
    source_uri = metadata.get("source_uri")
    attribution = metadata.get("attribution")
    cleared = (basis == "public_domain" and _is_https_provenance_uri(source_uri)) or (
        artifact_kind in {"image", "thumbnail", "captions"} and basis == "generated_in_application"
    )
    if basis == "cc0":
        cleared = (
            isinstance(license_id, str)
            and bool(license_id.strip())
            and _is_https_provenance_uri(license_uri)
            and _is_https_provenance_uri(source_uri)
        )
    if basis in {"licensed", "cc_by"}:
        cleared = (
            isinstance(license_id, str)
            and bool(license_id.strip())
            and _is_https_provenance_uri(license_uri)
            and _is_https_provenance_uri(source_uri)
            and isinstance(attribution, str)
            and bool(attribution.strip())
        )
    if cleared:
        return None
    return {
        "artifact_id": str(artifact_id),
        "artifact_kind": artifact_kind,
        "rights_basis": basis,
        "reason": "missing_or_incomplete_rights_provenance",
    }


_VTT_TIMESTAMP_PATTERN = re.compile(r"^(\d{2,}):(\d{2}):(\d{2})\.(\d{3})$")


def _background_music_rights_clearance_flag(
    *,
    artifact_id: UUID,
    metadata: object,
) -> dict | None:
    """Fail closed when rendered music has incomplete commercial-use provenance."""
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        return {
            "artifact_id": str(artifact_id),
            "artifact_kind": "audio",
            "reason": "background_music_metadata_invalid",
        }
    usage_scope = metadata.get("usage_scope")
    if (
        metadata.get("rights_status") != "cleared"
        or metadata.get("commercial_use") is not True
        or metadata.get("derivative_works") is not True
        or not isinstance(usage_scope, list)
        or not all(isinstance(scope, str) for scope in usage_scope)
        or not {"youtube", "commercial"}.issubset(usage_scope)
        or not isinstance(metadata.get("content_sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", metadata["content_sha256"]) is None
    ):
        return {
            "artifact_id": str(artifact_id),
            "artifact_kind": "audio",
            "reason": "background_music_rights_not_cleared",
        }
    return _rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={
            "rights_basis": "licensed",
            "license_id": metadata.get("license_id"),
            "license_uri": metadata.get("license_url"),
            "source_uri": metadata.get("source_url"),
            "attribution": metadata.get("attribution_text"),
        },
    )


def _vtt_timestamp_to_ms(value: str) -> int | None:
    match = _VTT_TIMESTAMP_PATTERN.fullmatch(value)
    if match is None:
        return None
    hours, minutes, seconds, milliseconds = (int(part) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        return None
    return (((hours * 60) + minutes) * 60 + seconds) * 1_000 + milliseconds


def _validate_caption_sidecar(captions_text: str, *, duration_ms: int) -> None:
    """Validate generated WebVTT cue structure and its timeline before review.

    The production captioner emits timestamp-only cue blocks, with at most
    seven words per cue. Keep this validator intentionally strict so malformed,
    overlapping, empty or out-of-range captions cannot reach editorial review.
    """
    if duration_ms <= 0 or not captions_text.startswith("WEBVTT\n"):
        raise MediaQualityGateError("Caption sidecar has an invalid WebVTT header.")

    blocks = [
        block.strip() for block in re.split(r"\n[ \t]*\n", captions_text.strip()) if block.strip()
    ]
    if not blocks or blocks[0] != "WEBVTT" or len(blocks) < 2:
        raise MediaQualityGateError("Caption sidecar must contain at least one cue.")

    previous_end_ms = 0
    for block in blocks[1:]:
        lines = block.splitlines()
        if not lines or " --> " not in lines[0]:
            raise MediaQualityGateError("Caption sidecar contains a malformed cue.")
        if len(lines) < 2:
            raise MediaQualityGateError("Caption cue text is empty or exceeds seven words.")
        start_text, end_text = lines[0].split(" --> ", maxsplit=1)
        start_ms = _vtt_timestamp_to_ms(start_text)
        end_ms = _vtt_timestamp_to_ms(end_text)
        cue_text = " ".join(line.strip() for line in lines[1:]).strip()
        if start_ms is None or end_ms is None:
            raise MediaQualityGateError("Caption sidecar contains an invalid timestamp.")
        if start_ms < previous_end_ms or end_ms <= start_ms:
            raise MediaQualityGateError("Caption sidecar contains overlapping or empty timing.")
        if end_ms > duration_ms:
            raise MediaQualityGateError("Caption cue extends beyond the rendered video.")
        is_silent_tail = cue_text == "\u200b"
        if not cue_text or (not is_silent_tail and len(cue_text.split()) > 7):
            raise MediaQualityGateError("Caption cue text is empty or exceeds seven words.")
        previous_end_ms = end_ms

    if previous_end_ms != duration_ms:
        raise MediaQualityGateError("Caption timeline does not cover the rendered video.")


async def execute_quality_check_stage(
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    renderer: VideoRenderer,
    storage: MediaStorage,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    direction = await _load_stage_manifest(
        session_factory,
        production_run_id=production_run_id,
        stage_key="ai_direction",
    )
    _, story = await _load_run_and_story(
        session_factory,
        organization_id=organization_id,
        production_run_id=production_run_id,
    )
    authoritative_source_rights = _source_rights_snapshot(story)
    source_rights_manifest_mismatch = direction.get("source_rights") != authoritative_source_rights
    source_rights_flags = _source_rights_clearance_flags(authoritative_source_rights)
    rendering = await _load_stage_manifest(
        session_factory,
        production_run_id=production_run_id,
        stage_key="rendering",
    )
    video_record = await _load_media_artifact(
        session_factory,
        organization_id=organization_id,
        production_run_id=production_run_id,
        artifact_id=UUID(rendering["video_artifact_id"]),
    )
    video_content = await _read_artifact(storage, video_record)
    report = await renderer.inspect(video_content)
    if not report.is_valid:
        raise MediaQualityGateError(
            "Rendered video failed technical QA: " + ",".join(report.issues)
        )
    if (
        report.width != video_record.width
        or report.height != video_record.height
        or abs(report.duration_ms - (video_record.duration_ms or 0)) > 750
    ):
        raise MediaQualityGateError(
            "Rendered video metadata does not match the registered artifact."
        )

    caption_record = await _load_media_artifact(
        session_factory,
        organization_id=organization_id,
        production_run_id=production_run_id,
        artifact_id=UUID(rendering["captions_artifact_id"]),
    )
    captions_content = await _read_artifact(storage, caption_record)
    try:
        captions_text = captions_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MediaQualityGateError("Caption sidecar is not valid UTF-8.") from exc
    if caption_record.mime_type != "text/vtt" or not captions_text.startswith("WEBVTT"):
        raise MediaQualityGateError("Caption sidecar is not a valid WebVTT artifact.")
    _validate_caption_sidecar(
        captions_text,
        duration_ms=int(direction.get("target_duration_seconds", 0)) * 1_000,
    )

    narration_manifest = await _load_stage_manifest(
        session_factory,
        production_run_id=production_run_id,
        stage_key="narration",
    )
    audio_record = await _load_media_artifact(
        session_factory,
        organization_id=organization_id,
        production_run_id=production_run_id,
        artifact_id=UUID(narration_manifest["audio_artifact_id"]),
    )
    visual_manifest = await _load_stage_manifest(
        session_factory,
        production_run_id=production_run_id,
        stage_key="visual_generation",
    )
    rights_flags = []
    attribution_sources = [audio_record]
    visual_records = []
    caption_flag = _rights_clearance_flag(
        artifact_id=caption_record.id,
        artifact_kind=caption_record.artifact_kind,
        metadata=caption_record.metadata_json,
    )
    if caption_flag is not None:
        rights_flags.append(caption_flag)
    audio_flag = _rights_clearance_flag(
        artifact_id=audio_record.id,
        artifact_kind=audio_record.artifact_kind,
        metadata=audio_record.metadata_json,
    )
    if audio_flag is not None:
        rights_flags.append(audio_flag)
    media_manifest_flags = []
    for scene in visual_manifest["scenes"]:
        visual_record = await _load_media_artifact(
            session_factory,
            organization_id=organization_id,
            production_run_id=production_run_id,
            artifact_id=UUID(scene["artifact_id"]),
        )
        visual_records.append(visual_record)
        visual_flag = _rights_clearance_flag(
            artifact_id=visual_record.id,
            artifact_kind=visual_record.artifact_kind,
            metadata=visual_record.metadata_json,
        )
        if visual_flag is not None:
            rights_flags.append(visual_flag)

    synthetic_values = [
        record.metadata_json.get("contains_synthetic_media") for record in visual_records
    ]
    if not all(isinstance(value, bool) for value in synthetic_values):
        media_manifest_flags.append(
            {
                "artifact_id": str(video_record.id),
                "artifact_kind": "video",
                "reason": "synthetic_media_manifest_value_invalid",
            }
        )
    contains_synthetic_media = any(value is True for value in synthetic_values)
    if video_record.metadata_json.get("contains_synthetic_media") is not contains_synthetic_media:
        media_manifest_flags.append(
            {
                "artifact_id": str(video_record.id),
                "artifact_kind": "video",
                "reason": "synthetic_media_manifest_mismatch",
            }
        )
    if rendering.get("contains_synthetic_media") is not contains_synthetic_media:
        media_manifest_flags.append(
            {
                "artifact_id": str(video_record.id),
                "artifact_kind": "video",
                "reason": "render_manifest_synthetic_media_mismatch",
            }
        )

    # Research-source rights are authoritative in the verified story artifact,
    # not in a model-produced scene plan or a mutable render manifest.
    rights_flags.extend(source_rights_flags)
    if source_rights_manifest_mismatch:
        rights_flags.append(
            {
                "artifact_id": str(video_record.id),
                "artifact_kind": "video",
                "reason": "source_rights_manifest_mismatch",
            }
        )

    # Rebuild attribution from the authoritative source artifacts. The render
    # manifest is a copy, not the source of truth: a missing or altered credit
    # must never turn a rights-cleared asset into a publishable video.
    attribution_credits = []
    for record in [*visual_records, *attribution_sources]:
        metadata = record.metadata_json
        if metadata.get("attribution"):
            attribution_credits.append(
                {
                    "artifact_id": str(record.id),
                    "provider_id": metadata.get("provider_id"),
                    "rights_basis": metadata.get("rights_basis"),
                    "source_uri": metadata.get("source_uri"),
                    "license_id": metadata.get("license_id"),
                    "license_uri": metadata.get("license_uri"),
                    "attribution": metadata.get("attribution"),
                }
            )
    attribution_credits.extend(_source_rights_attribution_credits(authoritative_source_rights))
    background_music_metadata = video_record.metadata_json.get("background_music")
    background_music_flag = _background_music_rights_clearance_flag(
        artifact_id=video_record.id,
        metadata=background_music_metadata,
    )
    if background_music_flag is not None:
        rights_flags.append(background_music_flag)
    elif isinstance(background_music_metadata, dict):
        attribution_credits.append(
            {
                "artifact_id": None,
                "provider_id": "background_music",
                "rights_basis": "licensed",
                "source_uri": background_music_metadata.get("source_url"),
                "license_id": background_music_metadata.get("license_id"),
                "license_uri": background_music_metadata.get("license_url"),
                "attribution": background_music_metadata.get("attribution_text"),
            }
        )

    recorded_credits = video_record.metadata_json.get("attribution_credits", [])
    if not isinstance(recorded_credits, list):
        recorded_credits = []
    if recorded_credits != attribution_credits:
        rights_flags.append(
            {
                "artifact_id": str(video_record.id),
                "artifact_kind": "video",
                "reason": "attribution_manifest_mismatch",
            }
        )
    if video_record.metadata_json.get("source_rights") != authoritative_source_rights:
        rights_flags.append(
            {
                "artifact_id": str(video_record.id),
                "artifact_kind": "video",
                "reason": "rendered_source_rights_manifest_mismatch",
            }
        )
    if video_record.metadata_json.get("rights_manifest_version") != "1.0.0":
        rights_flags.append(
            {
                "artifact_id": str(video_record.id),
                "artifact_kind": "video",
                "reason": "rendered_rights_manifest_version_mismatch",
            }
        )

    quality_report = {
        "is_valid": report.is_valid,
        "duration_ms": report.duration_ms,
        "width": report.width,
        "height": report.height,
        "video_codec": report.video_codec,
        "audio_codec": report.audio_codec,
        "has_audio": report.has_audio,
        "has_captions": report.has_captions,
        "integrated_loudness_lufs": getattr(report, "integrated_loudness_lufs", None),
        "true_peak_dbfs": getattr(report, "true_peak_dbfs", None),
        "black_frame_segments": [
            {"start_seconds": start, "end_seconds": end}
            for start, end in getattr(report, "black_frame_segments", ())
        ],
        "issues": list(report.issues),
    }
    completed = await complete_production_stage(
        production_stage_id,
        lease_token,
        output_manifest={
            "video_artifact_id": rendering["video_artifact_id"],
            "attribution_credits": attribution_credits,
            "source_rights": authoritative_source_rights,
            "rights_manifest_version": "1.0.0",
            "captions_artifact_id": rendering["captions_artifact_id"],
            "quality_report": quality_report,
            "claim_flags": direction.get("claim_flags", []),
            "rights_flags": rights_flags,
            "contains_synthetic_media": contains_synthetic_media,
            "media_manifest_flags": media_manifest_flags,
            "publication_blocked": (
                bool(direction.get("publication_blocked", False))
                or bool(rights_flags)
                or bool(media_manifest_flags)
            ),
        },
        provider_id="ffprobe",
        model_id="media-quality-gates.v2",
        prompt_version="media-quality.v2",
        session_factory=session_factory,
    )
    if not completed:
        raise MediaStageLeaseError("Quality-check stage lease was lost before completion.")


async def run_media_stage_once(
    worker_id: str,
    *,
    tts_provider: TTSProvider | TTSProviderRegistry,
    tts_voice: str,
    tts_speed: float = 1.0,
    tts_audio_format: str = "wav",
    tts_rights_basis: str,
    tts_license_id: str | None,
    tts_license_uri: str | None,
    tts_source_uri: str | None,
    tts_attribution: str | None,
    visual_provider: VisualProvider,
    renderer: VideoRenderer,
    storage: MediaStorage,
    session_factory: async_sessionmaker[AsyncSession],
    background_music_provider: BackgroundMusicProvider | None = None,
    alignment_provider: WhisperXAlignmentProvider | None = None,
    lease_seconds: int = 300,
) -> bool:
    """Claim and execute at most one media stage using durable PostgreSQL leases."""

    claimed = await claim_production_stage(
        worker_id,
        lease_seconds=lease_seconds,
        stage_keys=MEDIA_STAGE_KEYS,
        session_factory=session_factory,
    )
    if claimed is None:
        return False

    run_id, stage_id, token, attempt_count = claimed
    lease_token = UUID(token)
    async with session_factory() as session:
        stage = await session.get(ProductionStage, stage_id)
        run = await session.get(ProductionRun, run_id)
        if stage is None or run is None:
            raise LookupError("Claimed media stage or run disappeared.")
        stage_key = stage.stage_key
        organization_id = run.organization_id

    stop_heartbeat = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _maintain_stage_lease(
            stage_id,
            lease_token,
            lease_seconds=lease_seconds,
            stop=stop_heartbeat,
            lost=lease_lost,
            session_factory=session_factory,
        )
    )
    try:
        common = {
            "organization_id": organization_id,
            "production_run_id": run_id,
            "production_stage_id": stage_id,
            "lease_token": lease_token,
            "session_factory": session_factory,
        }
        if stage_key == "ai_direction":
            await execute_ai_direction_stage(**common)
        elif stage_key == "narration":
            await execute_narration_stage(
                **common,
                attempt_count=attempt_count,
                provider=tts_provider,
                voice_id=tts_voice,
                speed=tts_speed,
                audio_format=tts_audio_format,
                rights_basis=tts_rights_basis,
                license_id=tts_license_id,
                license_uri=tts_license_uri,
                source_uri=tts_source_uri,
                attribution=tts_attribution,
                storage=storage,
            )
        elif stage_key == "visual_generation":
            await execute_visual_generation_stage(
                **common,
                attempt_count=attempt_count,
                provider=visual_provider,
                storage=storage,
            )
        elif stage_key == "rendering":
            await execute_rendering_stage(
                **common,
                attempt_count=attempt_count,
                renderer=renderer,
                storage=storage,
                background_music_provider=background_music_provider,
                alignment_provider=alignment_provider,
            )
        elif stage_key == "quality_check":
            await execute_quality_check_stage(
                **common,
                renderer=renderer,
                storage=storage,
            )
        else:
            raise RuntimeError(f"Unsupported media worker stage: {stage_key}")
    finally:
        stop_heartbeat.set()
        await heartbeat

    if lease_lost.is_set():
        raise MediaStageLeaseError("Media production stage lease was lost during execution.")
    return True
