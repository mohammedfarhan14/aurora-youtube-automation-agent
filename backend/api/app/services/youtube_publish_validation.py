"""Revalidation of approved YouTube publishing snapshots before provider operations."""

from uuid import UUID

from app.services.youtube_oauth import channel_can_publish
from app.services.youtube_publishing import (
    UNVERIFIED_LICENSE_LINE,
    YouTubePublishLeaseLostError,
)
from app.services.youtube_tokens import YouTubeReauthorizationRequired
from app.services.youtube_upload import YouTubeUploadPermanentError


async def validate_youtube_publish_job_snapshot(
    session_factory,
    *,
    job_id: UUID,
    lease_token: UUID,
) -> None:
    """Recheck approval, channel, immutable render and rights before every provider call."""
    from sqlalchemy import func, select

    from app.models.media_artifact import MediaArtifact
    from app.models.production_run import ProductionRun
    from app.models.production_stage import ProductionStage
    from app.models.youtube import YouTubeChannel
    from app.models.youtube_publishing import YouTubePublishJob

    async with session_factory() as session:
        job = await session.scalar(
            select(YouTubePublishJob).where(
                YouTubePublishJob.id == job_id,
                YouTubePublishJob.lease_token == lease_token,
                YouTubePublishJob.lease_expires_at > func.clock_timestamp(),
            )
        )
        if job is None:
            raise YouTubePublishLeaseLostError(
                "YouTube publishing worker no longer owns this job."
            )
        if job.status not in {"queued", "initiating", "uploading", "processing", "uploaded"}:
            raise YouTubeUploadPermanentError(
                "The publishing job is no longer in an executable state."
            )
        run = await session.scalar(
            select(ProductionRun).where(
                ProductionRun.id == job.production_run_id,
                ProductionRun.organization_id == job.organization_id,
            )
        )
        channel = await session.scalar(
            select(YouTubeChannel).where(
                YouTubeChannel.id == job.youtube_channel_id,
                YouTubeChannel.organization_id == job.organization_id,
            )
        )
        artifact = await session.scalar(
            select(MediaArtifact).where(
                MediaArtifact.id == job.media_artifact_id,
                MediaArtifact.organization_id == job.organization_id,
                MediaArtifact.production_run_id == job.production_run_id,
                MediaArtifact.artifact_kind == "video",
            )
        )
        if run is None or channel is None or artifact is None:
            raise YouTubeUploadPermanentError(
                "The approved production, channel or rendered artifact is unavailable."
            )
        if (
            run.status != "approved"
            or run.approval_mode != job.approval_mode
            or run.approved_at is None
            or run.approved_at != job.approved_at
            or run.youtube_channel_id != job.youtube_channel_id
            or run.approval_consent_event_id != job.approval_consent_event_id
        ):
            raise YouTubeUploadPermanentError(
                "The production run no longer matches its recorded approval snapshot."
            )
        if run.approval_mode == "human":
            if (
                run.approved_by_user_id is None
                or run.approved_by_user_id != job.approved_by_user_id
                or job.approval_consent_event_id is not None
            ):
                raise YouTubeUploadPermanentError(
                    "The production run no longer matches its recorded human approval."
                )
        elif run.approval_mode == "automatic_consent":
            if run.approved_by_user_id is not None or job.approved_by_user_id is not None:
                raise YouTubeUploadPermanentError(
                    "Automatic consent approval cannot be represented as a human approval."
                )
        else:
            raise YouTubeUploadPermanentError("The production approval mode is invalid.")
        if channel.status != "active" or not channel_can_publish(
            set((channel.granted_scopes or "").split())
        ):
            raise YouTubeReauthorizationRequired(
                "The connected channel is inactive or no longer has upload permission."
            )
        if job.approval_mode == "automatic_consent":
            from app.services.youtube_publishing import (
                get_current_automatic_publishing_consent,
            )

            consent_event = await get_current_automatic_publishing_consent(
                session, channel=channel
            )
            expected_privacy_status = channel.auto_publish_privacy_status
            if job.publish_at is not None:
                if channel.auto_publish_privacy_status != "public":
                    raise YouTubeUploadPermanentError(
                        "Scheduled automatic publishing requires public channel visibility."
                    )
                expected_privacy_status = "private"
            if (
                consent_event is None
                or consent_event.id != job.approval_consent_event_id
                or consent_event.id != run.approval_consent_event_id
                or consent_event.actor_user_id != job.requested_by_user_id
                or job.privacy_status != expected_privacy_status
            ):
                raise YouTubeUploadPermanentError(
                    "Automatic publishing consent is no longer active for this channel."
                )
        if (
            artifact.mime_type != "video/mp4"
            or artifact.storage_backend != job.storage_backend
            or artifact.storage_key != job.storage_key
            or artifact.sha256 != job.sha256
            or artifact.byte_size != job.byte_size
            or artifact.byte_size <= 0
        ):
            raise YouTubeUploadPermanentError(
                "The publishing job no longer matches its immutable rendered MP4 artifact."
            )

        stages = await session.scalars(
            select(ProductionStage).where(
                ProductionStage.production_run_id == run.id,
                ProductionStage.stage_key.in_(("rendering", "quality_check")),
                ProductionStage.status == "completed",
            )
        )
        stage_by_key = {stage.stage_key: stage for stage in stages.all()}
        rendering = stage_by_key.get("rendering")
        quality = stage_by_key.get("quality_check")
        if rendering is None or quality is None:
            raise YouTubeUploadPermanentError(
                "Completed rendering and final quality-check stages are required."
            )
        render_manifest = rendering.output_manifest
        quality_manifest = quality.output_manifest
        if not isinstance(render_manifest, dict) or not isinstance(quality_manifest, dict):
            raise YouTubeUploadPermanentError("The final production manifests are invalid.")
        quality_report = quality_manifest.get("quality_report")
        contains_synthetic_media = quality_manifest.get("contains_synthetic_media")
        media_manifest_flags = quality_manifest.get("media_manifest_flags")
        quality_issues = (
            quality_report.get("issues") if isinstance(quality_report, dict) else None
        )
        claim_flags = quality_manifest.get("claim_flags")
        rights_flags = quality_manifest.get("rights_flags")
        warning_override = run.approval_warning_override
        warning_override_valid = bool(
            isinstance(warning_override, dict)
            and run.approval_mode == "human"
            and run.approved_by_user_id is not None
            and warning_override.get("accepted_by_user_id") == str(run.approved_by_user_id)
            and isinstance(warning_override.get("reason"), str)
            and len(warning_override["reason"].strip()) >= 10
            and warning_override.get("claim_flags") == claim_flags
            and warning_override.get("rights_flags") == rights_flags
            and warning_override.get("publication_blocked")
            is quality_manifest.get("publication_blocked")
            and bool(claim_flags or rights_flags)
        )
        editorial_gate_clear = (
            quality_manifest.get("publication_blocked") is False
            and claim_flags == []
            and rights_flags == []
        )
        artifact_metadata = artifact.metadata_json
        metadata_snapshot = job.metadata_snapshot
        if (
            not isinstance(metadata_snapshot, dict)
            or metadata_snapshot.get("approval_mode") != job.approval_mode
            or metadata_snapshot.get("approval_consent_event_id")
            != (
                str(job.approval_consent_event_id)
                if job.approval_consent_event_id else None
            )
            or metadata_snapshot.get("auto_publish_privacy_status")
            != (
                channel.auto_publish_privacy_status
                if job.approval_mode == "automatic_consent" else None
            )
            or not isinstance(contains_synthetic_media, bool)
            or not isinstance(media_manifest_flags, list)
            or media_manifest_flags
            or not isinstance(quality_report, dict)
            or quality_report.get("is_valid") is not True
            or not isinstance(quality_issues, list)
            or quality_issues
            or not isinstance(claim_flags, list)
            or not isinstance(rights_flags, list)
            or not (editorial_gate_clear or warning_override_valid)
            or metadata_snapshot.get("manual_warning_override")
            != (warning_override if warning_override_valid else None)
            or artifact.production_stage_id != rendering.id
            or render_manifest.get("video_artifact_id") != str(artifact.id)
            or quality_manifest.get("video_artifact_id") != str(artifact.id)
            or not isinstance(artifact_metadata, dict)
            or not isinstance(metadata_snapshot, dict)
            or artifact_metadata.get("rights_manifest_version") != "1.0.0"
            or metadata_snapshot.get("rights_manifest_version")
            != artifact_metadata.get("rights_manifest_version")
            or artifact_metadata.get("attribution_credits")
            != quality_manifest.get("attribution_credits")
            or artifact_metadata.get("source_rights") != quality_manifest.get("source_rights")
            or metadata_snapshot.get("attribution_credits")
            != quality_manifest.get("attribution_credits")
            or metadata_snapshot.get("source_rights") != quality_manifest.get("source_rights")
            or metadata_snapshot.get("quality_report") != quality_report
            or metadata_snapshot.get("contains_synthetic_media")
            is not contains_synthetic_media
            or artifact_metadata.get("contains_synthetic_media")
            is not contains_synthetic_media
            or render_manifest.get("contains_synthetic_media")
            is not contains_synthetic_media
        ):
            raise YouTubeUploadPermanentError(
                "The current render, quality and rights manifests no longer match approval."
            )

        thumbnail_snapshot = metadata_snapshot.get("thumbnail_artifact")
        if thumbnail_snapshot is not None:
            if not isinstance(thumbnail_snapshot, dict):
                raise YouTubeUploadPermanentError("The approved thumbnail snapshot is invalid.")
            try:
                thumbnail_id = UUID(thumbnail_snapshot.get("artifact_id", ""))
            except (TypeError, ValueError) as exc:
                raise YouTubeUploadPermanentError(
                    "The approved thumbnail artifact identifier is invalid."
                ) from exc
            thumbnail = await session.scalar(
                select(MediaArtifact).where(
                    MediaArtifact.id == thumbnail_id,
                    MediaArtifact.organization_id == job.organization_id,
                    MediaArtifact.production_run_id == run.id,
                    MediaArtifact.artifact_kind == "thumbnail",
                )
            )
            thumbnail_metadata = thumbnail.metadata_json if thumbnail is not None else None
            if (
                thumbnail is None
                or render_manifest.get("thumbnail_artifact_id") != str(thumbnail.id)
                or thumbnail.mime_type not in {"image/jpeg", "image/png"}
                or thumbnail.storage_backend != thumbnail_snapshot.get("storage_backend")
                or thumbnail.storage_key != thumbnail_snapshot.get("storage_key")
                or thumbnail.sha256 != thumbnail_snapshot.get("sha256")
                or thumbnail.byte_size != thumbnail_snapshot.get("byte_size")
                or not 0 < thumbnail.byte_size <= 50_000_000
                or not isinstance(thumbnail_metadata, dict)
                or thumbnail_metadata.get("generated_by") != "aurora-thumbnail.v1"
            ):
                raise YouTubeUploadPermanentError(
                    "The approved thumbnail no longer matches its rendering artifact."
                )

        source_rights = quality_manifest.get("source_rights")
        if not isinstance(source_rights, list) or not source_rights:
            raise YouTubeUploadPermanentError("The final source-rights manifest is missing.")
        for source in source_rights:
            if not isinstance(source, dict):
                raise YouTubeUploadPermanentError(
                    "A source in the final rights manifest is invalid."
                )
            if (
                not isinstance(source.get("canonical_url"), str)
                or not source["canonical_url"].startswith("https://")
                or not isinstance(source.get("attribution_text"), str)
                or not source["attribution_text"].strip()
            ):
                raise YouTubeUploadPermanentError(
                    "A source in the final rights manifest lacks a safe URL or attribution."
                )
            license_ids = source.get("license_ids")
            license_urls = source.get("license_urls")
            valid_license_refs = (
                isinstance(license_ids, list)
                and bool(license_ids)
                and isinstance(license_urls, list)
                and len(license_ids) == len(license_urls)
                and all(
                    isinstance(license_id, str)
                    and bool(license_id.strip())
                    and isinstance(license_url, str)
                    and license_url.startswith("https://")
                    for license_id, license_url in zip(license_ids, license_urls, strict=True)
                )
            )
            if (
                not warning_override_valid
                and (
                    source.get("status") != "cleared"
                    or source.get("scope") in (None, "unknown")
                    or not valid_license_refs
                )
            ):
                raise YouTubeUploadPermanentError(
                    "A source in the final rights manifest is not cleared for reuse."
                )

        credits = quality_manifest.get("attribution_credits")
        if (
            not isinstance(credits, list)
            or not credits
            or not isinstance(job.description, str)
            or "Sources and credits" not in job.description
        ):
            raise YouTubeUploadPermanentError(
                "The approved YouTube description is missing mandatory source credits."
            )
        for credit in credits:
            if not isinstance(credit, dict):
                raise YouTubeUploadPermanentError("The attribution manifest is invalid.")
            attribution = credit.get("attribution")
            if (
                not isinstance(attribution, str)
                or not attribution.strip()
                or attribution.strip() not in job.description
            ):
                raise YouTubeUploadPermanentError(
                    "The approved YouTube description is missing an attribution."
                )
            license_ids = credit.get("license_ids")
            license_urls = credit.get("license_urls")
            valid_license_refs = (
                isinstance(license_ids, list)
                and bool(license_ids)
                and isinstance(license_urls, list)
                and len(license_ids) == len(license_urls)
                and all(
                    isinstance(license_id, str)
                    and bool(license_id.strip())
                    and isinstance(license_url, str)
                    and license_url.startswith("https://")
                    for license_id, license_url in zip(license_ids, license_urls, strict=True)
                )
            )
            if valid_license_refs:
                required_license_lines = [
                    f"License: {license_id} — {license_url}"
                    for license_id, license_url in zip(license_ids, license_urls, strict=True)
                ]
            else:
                license_id = credit.get("license_id")
                license_uri = credit.get("license_uri")
                valid_license_ref = (
                    isinstance(license_id, str)
                    and bool(license_id.strip())
                    and isinstance(license_uri, str)
                    and license_uri.startswith("https://")
                )
                if valid_license_ref:
                    required_license_lines = [f"License: {license_id} — {license_uri}"]
                elif warning_override_valid:
                    required_license_lines = [UNVERIFIED_LICENSE_LINE]
                else:
                    raise YouTubeUploadPermanentError(
                        "A rendered credit is missing its license reference."
                    )
            if any(line not in job.description for line in required_license_lines):
                raise YouTubeUploadPermanentError(
                    "The approved YouTube description is missing a license reference."
                )
            source_uri = credit.get("source_uri")
            if source_uri is not None and source_uri != "":
                if (
                    not isinstance(source_uri, str)
                    or not source_uri.startswith("https://")
                    or f"Source: {source_uri}" not in job.description
                ):
                    raise YouTubeUploadPermanentError(
                        "The approved YouTube description is missing a safe source URL."
                    )
            modification_note = credit.get("modification_note")
            if (
                isinstance(modification_note, str)
                and modification_note
                and modification_note not in job.description
            ):
                raise YouTubeUploadPermanentError(
                    "The approved YouTube description is missing a modification note."
                )

