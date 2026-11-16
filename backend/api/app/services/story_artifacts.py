"""Tenant-scoped append-only persistence for validated story artifacts."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage
from app.models.story_artifact import StoryArtifactRecord
from app.story.contracts import StoryArtifact

StoryArtifactType = Literal["story_draft", "story_verified"]


class StoryArtifactConflictError(ValueError):
    """The artifact does not match the current run, stage, or lease."""


def _canonical_payload(artifact: StoryArtifact) -> tuple[dict, str]:
    payload = artifact.model_dump(mode="json")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload, hashlib.sha256(canonical).hexdigest()


async def persist_story_artifact_and_complete_stage(
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    artifact: StoryArtifact,
    artifact_type: StoryArtifactType,
    inference_provenance: dict | None = None,
    session_factory: async_sessionmaker[AsyncSession],
) -> StoryArtifactRecord | None:
    """Atomically append an artifact and complete its currently leased stage.

    A stale or expired lease cannot write an artifact. Cancellation wins over
    completion: the generated in-memory result is discarded and pending stages
    are cancelled in the same transaction.
    """

    if not isinstance(artifact, StoryArtifact):
        raise TypeError("artifact must be a validated StoryArtifact.")
    expected_stage = {
        "story_draft": "story_generation",
        "story_verified": "claim_verification",
    }[artifact_type]
    payload, content_hash = _canonical_payload(artifact)
    provenance = inference_provenance or {}
    json.dumps(provenance, ensure_ascii=False, sort_keys=True)

    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:
                raise RuntimeError("Could not obtain the database clock.")

            stage = await session.scalar(
                select(ProductionStage)
                .where(
                    ProductionStage.id == production_stage_id,
                    ProductionStage.production_run_id == production_run_id,
                    ProductionStage.stage_key == expected_stage,
                    ProductionStage.status == "running",
                    ProductionStage.lease_token == lease_token,
                    ProductionStage.lease_expires_at > now,
                )
                .with_for_update()
            )
            if stage is None:
                raise StoryArtifactConflictError(
                    "The stage lease is missing, expired, or no longer owned."
                )

            run = await session.scalar(
                select(ProductionRun)
                .where(
                    ProductionRun.id == production_run_id,
                    ProductionRun.organization_id == organization_id,
                )
                .with_for_update()
            )
            if run is None:
                raise StoryArtifactConflictError(
                    "Production run does not belong to this organization."
                )

            if run.cancel_requested_at is not None:
                stage.status = "cancelled"
                stage.lease_token = None
                stage.lease_expires_at = None
                stage.completed_at = now
                pending = await session.scalars(
                    select(ProductionStage).where(
                        ProductionStage.production_run_id == run.id,
                        ProductionStage.status == "pending",
                    )
                )
                for pending_stage in pending:
                    pending_stage.status = "cancelled"
                    pending_stage.completed_at = now
                run.status = "cancelled"
                run.completed_at = now
                return None

            latest_version = await session.scalar(
                select(func.max(StoryArtifactRecord.version)).where(
                    StoryArtifactRecord.organization_id == organization_id,
                    StoryArtifactRecord.production_run_id == production_run_id,
                    StoryArtifactRecord.artifact_type == artifact_type,
                )
            )
            record = StoryArtifactRecord(
                organization_id=organization_id,
                production_run_id=production_run_id,
                production_stage_id=production_stage_id,
                artifact_type=artifact_type,
                schema_version=artifact.schema_version,
                version=(latest_version or 0) + 1,
                content_hash=content_hash,
                payload=payload,
            )
            session.add(record)
            await session.flush()

            stage.provider_id = str(provenance.get("provider") or "unknown")[:100]
            stage.model_id = str(provenance.get("model") or "unknown")[:200]
            stage.prompt_version = str(provenance.get("prompt_version") or "unknown")[:100]
            request_id = provenance.get("request_id")
            stage.request_id = str(request_id)[:200] if request_id else None
            stage.status = "completed"
            stage.output_manifest = {
                "artifact_id": str(record.id),
                "artifact_type": artifact_type,
                "artifact_version": record.version,
                "schema_version": record.schema_version,
                "content_hash": content_hash,
                "inference_provenance": provenance,
            }
            stage.lease_token = None
            stage.lease_expires_at = None
            stage.completed_at = now
            stage.failure_code = None
            stage.failure_message = None
            return record


async def load_latest_story_artifact(
    session: AsyncSession,
    *,
    organization_id: UUID,
    production_run_id: UUID,
    artifact_type: StoryArtifactType,
) -> StoryArtifact:
    """Load and revalidate the latest artifact inside the requesting tenant."""

    record = await session.scalar(
        select(StoryArtifactRecord)
        .where(
            StoryArtifactRecord.organization_id == organization_id,
            StoryArtifactRecord.production_run_id == production_run_id,
            StoryArtifactRecord.artifact_type == artifact_type,
        )
        .order_by(StoryArtifactRecord.version.desc())
        .limit(1)
    )
    if record is None:
        raise StoryArtifactConflictError("Required story artifact does not exist.")

    canonical = json.dumps(
        record.payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != record.content_hash:
        raise StoryArtifactConflictError("Stored story artifact hash does not match its payload.")

    try:
        return StoryArtifact.model_validate_json(json.dumps(record.payload, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise StoryArtifactConflictError(
            "Stored story artifact no longer matches its schema."
        ) from exc
