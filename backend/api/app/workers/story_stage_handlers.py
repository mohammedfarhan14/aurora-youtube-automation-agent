"""Execution handlers for the evidence-backed story production stages.

These handlers deliberately accept provider instances and model identifiers from
trusted worker configuration. Tenant and research identifiers always come from
the claimed production run, never from model output.
"""

import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.contracts import LLMProvider
from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage
from app.services.production import (
    claim_production_stage,
    record_production_stage_failure,
    renew_production_stage_lease,
)
from app.services.story_artifacts import (
    StoryArtifactRecord,
    load_latest_story_artifact,
    persist_story_artifact_and_complete_stage,
)
from app.story.context import StoryResearchContext, load_story_research_context
from app.story.generation import generate_story
from app.story.verification import verify_story_claims
from app.workers.story_skill_runtime import execute_audited_story_operation

STORY_STAGE_KEYS = ("story_generation", "claim_verification")
_STORY_CONTEXT_LIMITS = {
    "max_sources": 4,
    "max_evidence_per_source": 1,
    "max_context_characters": 10_000,
}


async def _load_production_story_context(
    session: AsyncSession,
    *,
    organization_id: UUID,
    run: ProductionRun,
) -> StoryResearchContext:
    """Use identical source/evidence selection for generation and verification."""
    return await load_story_research_context(
        session,
        organization_id=organization_id,
        research_run_id=run.research_run_id,
        editorial_profile=run.channel_profile_snapshot,
        youtube_channel_id=run.youtube_channel_id,
        **_STORY_CONTEXT_LIMITS,
    )


async def _maintain_stage_lease(
    stage_id: UUID,
    lease_token: UUID,
    *,
    lease_seconds: int,
    stop: asyncio.Event,
    lost: asyncio.Event,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Renew a live stage lease periodically until execution finishes or ownership is lost."""

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
                lost.set()
                return


async def execute_story_generation_stage(
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    provider: LLMProvider,
    model: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> StoryArtifactRecord | None:
    """Generate and persist a source-linked draft under the active stage lease."""

    async with session_factory() as session:
        run = await session.scalar(
            select(ProductionRun).where(
                ProductionRun.id == production_run_id,
                ProductionRun.organization_id == organization_id,
            )
        )
        if run is None:
            raise LookupError("Production run not found in this organization.")
        actor_id = run.requested_by_user_id
        preferences = (run.channel_profile_snapshot or {}).get("_workspace_preferences", {})
        target_duration_seconds = (
            preferences.get("target_duration_seconds", 30) if isinstance(preferences, dict) else 30
        )
        qwen_thinking_mode = (
            preferences.get("qwen_thinking_mode", "off") if isinstance(preferences, dict) else "off"
        )
        max_output_tokens = (
            preferences.get("story_max_output_tokens") if isinstance(preferences, dict) else None
        )
        timeout_seconds = (
            preferences.get("story_timeout_seconds", 120) if isinstance(preferences, dict) else 120
        )
        context = await _load_production_story_context(
            session,
            organization_id=organization_id,
            run=run,
        )

    async def generate() -> object:
        return await generate_story(
            provider,
            context,
            model=model,
            target_duration_seconds=target_duration_seconds,
            qwen_thinking_mode=qwen_thinking_mode,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

    result = await execute_audited_story_operation(
        skill_id="story.generation",
        tool_id="story.generate",
        organization_id=organization_id,
        actor_id=actor_id,
        request_id=f"production-stage:{production_stage_id}",
        idempotency_key=f"production-stage:{production_stage_id}",
        operation=generate,
        session_factory=session_factory,
    )
    return await persist_story_artifact_and_complete_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        artifact=result.artifact,
        artifact_type="story_draft",
        inference_provenance={
            "provider": result.provider,
            "model": result.model,
            "prompt_version": result.prompt_version,
            "request_id": result.request_id,
            "usage": result.usage.model_dump(mode="json"),
        },
        session_factory=session_factory,
    )


async def execute_claim_verification_stage(
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    extraction_provider: LLMProvider,
    verification_provider: LLMProvider,
    extraction_model: str,
    verification_model: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> StoryArtifactRecord | None:
    """Verify every atomic claim in the persisted draft against retrieved evidence."""

    async with session_factory() as session:
        run = await session.scalar(
            select(ProductionRun).where(
                ProductionRun.id == production_run_id,
                ProductionRun.organization_id == organization_id,
            )
        )
        if run is None:
            raise LookupError("Production run not found in this organization.")
        actor_id = run.requested_by_user_id
        preferences = (run.channel_profile_snapshot or {}).get("_workspace_preferences", {})
        timeout_seconds = (
            preferences.get("story_timeout_seconds", 120) if isinstance(preferences, dict) else 120
        )
        qwen_thinking_mode = (
            preferences.get("qwen_thinking_mode", "off") if isinstance(preferences, dict) else "off"
        )
        draft = await load_latest_story_artifact(
            session,
            organization_id=organization_id,
            production_run_id=production_run_id,
            artifact_type="story_draft",
        )
        context = await _load_production_story_context(
            session,
            organization_id=organization_id,
            run=run,
        )

    async def verify() -> object:
        return await verify_story_claims(
            extraction_provider,
            verification_provider,
            draft,
            context,
            extraction_model=extraction_model,
            verification_model=verification_model,
            timeout_seconds=timeout_seconds,
            thinking_enabled=(
                qwen_thinking_mode == "on" if "qwen3" in extraction_model.casefold() else None
            ),
        )

    result = await execute_audited_story_operation(
        skill_id="story.claim_verification",
        tool_id="story.verify_claims",
        organization_id=organization_id,
        actor_id=actor_id,
        request_id=f"production-stage:{production_stage_id}",
        idempotency_key=f"production-stage:{production_stage_id}",
        operation=verify,
        session_factory=session_factory,
    )
    return await persist_story_artifact_and_complete_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        artifact=result.artifact,
        artifact_type="story_verified",
        inference_provenance={
            "provider": "multi-stage",
            "model": f"{extraction_model}+{verification_model}",
            "prompt_version": (
                f"{result.extraction_prompt_version}+{result.verification_prompt_version}"
            ),
            "extraction": {
                "provider": result.extraction_provider,
                "model": result.extraction_model,
                "prompt_version": result.extraction_prompt_version,
                "request_id": result.extraction_request_id,
            },
            "verification": {
                "provider": result.verification_provider,
                "model": result.verification_model,
                "prompt_version": result.verification_prompt_version,
                "request_id": result.verification_request_id,
            },
        },
        session_factory=session_factory,
    )


async def run_story_stage_once(
    worker_id: str,
    *,
    provider: LLMProvider,
    model: str,
    verification_model: str,
    session_factory: async_sessionmaker[AsyncSession],
    lease_seconds: int = 300,
    max_attempts: int = 5,
) -> bool:
    """Claim and execute at most one story-generation or claim-verification stage.

    Other production stages remain queued for their own specialized workers.
    Known execution failures release their lease and requeue with backoff;
    abrupt worker loss remains covered by durable lease-expiration recovery.
    """

    claimed = await claim_production_stage(
        worker_id,
        lease_seconds=lease_seconds,
        stage_keys=STORY_STAGE_KEYS,
        session_factory=session_factory,
    )
    if claimed is None:
        return False

    run_id, stage_id, token, _ = claimed
    lease_token = UUID(token)
    async with session_factory() as session:
        stage = await session.get(ProductionStage, stage_id)
        run = await session.get(ProductionRun, run_id)
        if stage is None or run is None:
            raise LookupError("Claimed production stage or run disappeared.")
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
    stage_error: Exception | None = None
    try:
        if stage_key == "story_generation":
            await execute_story_generation_stage(
                organization_id=organization_id,
                production_run_id=run_id,
                production_stage_id=stage_id,
                lease_token=lease_token,
                provider=provider,
                model=model,
                session_factory=session_factory,
            )
        elif stage_key == "claim_verification":
            await execute_claim_verification_stage(
                organization_id=organization_id,
                production_run_id=run_id,
                production_stage_id=stage_id,
                lease_token=lease_token,
                extraction_provider=provider,
                verification_provider=provider,
                extraction_model=model,
                verification_model=verification_model,
                session_factory=session_factory,
            )
        else:
            raise RuntimeError(f"Unsupported story worker stage: {stage_key}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        stage_error = exc
    finally:
        stop_heartbeat.set()
        await heartbeat

    if stage_error is not None:
        await record_production_stage_failure(
            stage_id,
            lease_token,
            max_attempts=max_attempts,
            failure_code="story_stage_execution_failed",
            failure_message="Story stage execution failed; retry or operator review is required.",
            session_factory=session_factory,
        )
        raise stage_error

    if lease_lost.is_set():
        raise RuntimeError("Production stage lease was lost during execution.")

    return True
