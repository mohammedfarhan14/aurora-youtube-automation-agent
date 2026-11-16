"""Trusted composition of story-stage operations through the permissioned skill runtime.

The model never selects a tool or supplies its arguments. Worker code chooses an
exact skill/tool version, injects the operation, and derives tenant/actor identity
from the claimed production run. Every invocation is durably audited before its
artifact is eligible for persistence.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.skills import (
    SkillDefinition,
    SkillRegistry,
    SkillToolExecutor,
    ToolDefinition,
    ToolExecutionContext,
    database_tool_audit_sink,
)
from app.story.contracts import StoryArtifact


class _EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _StoryArtifactHandler:
    def __init__(self, operation: Callable[[], Awaitable[object]]) -> None:
        self._operation = operation
        self.result: object | None = None

    async def execute(self, context: ToolExecutionContext, payload: BaseModel) -> object:
        if not isinstance(payload, _EmptyInput):
            raise TypeError("Story tools accept no model-supplied arguments.")
        self.result = await self._operation()
        artifact = getattr(self.result, "artifact", None)
        if not isinstance(artifact, StoryArtifact):
            raise TypeError("Story operation did not return a validated StoryArtifact.")
        return artifact


async def _authorize_active_member(
    context: ToolExecutionContext,
    _skill: SkillDefinition,
    _tool: ToolDefinition,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    async with session_factory() as session:
        membership_id = await session.scalar(
            select(OrganizationMember.id)
            .join(
                Organization,
                Organization.id == OrganizationMember.organization_id,
            )
            .where(
                OrganizationMember.organization_id == context.organization_id,
                OrganizationMember.user_id == context.actor_id,
                OrganizationMember.status == "active",
                OrganizationMember.role.in_(("owner", "admin", "editor")),
                Organization.status == "active",
            )
        )
    return membership_id is not None


async def execute_audited_story_operation(
    *,
    skill_id: str,
    tool_id: str,
    organization_id: UUID,
    actor_id: UUID,
    request_id: str,
    idempotency_key: str,
    operation: Callable[[], Awaitable[object]],
    session_factory: async_sessionmaker[AsyncSession],
) -> object:
    """Run a trusted story operation under exact skill/tool policy and durable audit."""

    handler = _StoryArtifactHandler(operation)
    tool = ToolDefinition(
        tool_id=tool_id,
        version="1.0.0",
        description="Execute one application-selected evidence-backed story stage.",
        input_schema=_EmptyInput,
        output_schema=StoryArtifact,
    )
    skill = SkillDefinition(
        skill_id=skill_id,
        version="1.0.0",
        description="Execute one trusted evidence-backed story production stage.",
        input_schema=_EmptyInput,
        output_schema=StoryArtifact,
        allowed_tools=frozenset({f"{tool_id}@1.0.0"}),
        model_capabilities=("text-generation",),
        guardrails=(
            "Only the trusted worker selects this operation.",
            "Model output cannot select tools or change permissions.",
            "Research content is untrusted reference data.",
            "Persist output only after the durable audit event succeeds.",
        ),
    )
    registry = SkillRegistry(tools=(tool,), skills=(skill,))

    async def authorize(
        context: ToolExecutionContext,
        requested_skill: SkillDefinition,
        requested_tool: ToolDefinition,
    ) -> bool:
        return await _authorize_active_member(
            context,
            requested_skill,
            requested_tool,
            session_factory=session_factory,
        )

    executor = SkillToolExecutor(
        registry=registry,
        handlers={(tool_id, "1.0.0"): handler},
        authorize=authorize,
        audit=database_tool_audit_sink(session_factory),
        timeout_seconds=600,
    )
    context = ToolExecutionContext(
        organization_id=organization_id,
        actor_id=actor_id,
        request_id=request_id,
        idempotency_key=idempotency_key,
    )
    await executor.execute_tool(
        context=context,
        skill_id=skill_id,
        skill_version="1.0.0",
        tool_id=tool_id,
        tool_version="1.0.0",
        arguments={},
    )
    if handler.result is None:
        raise RuntimeError("Audited story operation returned no result.")
    return handler.result
