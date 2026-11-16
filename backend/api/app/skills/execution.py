"""Permissioned execution for explicitly declared, versioned skill tools.

The executor never interprets model-generated tool names as permissions. It resolves
exact versions through the registry, checks the skill allowlist, authorizes the
trusted tenant context, validates both sides of the tool boundary, and emits
content-free audit metadata.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.skills.contracts import SkillDefinition, ToolDefinition
from app.skills.registry import SkillRegistry


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Trusted identity and idempotency metadata supplied by the application."""

    organization_id: UUID
    actor_id: UUID
    request_id: str
    idempotency_key: str


class ToolHandler(Protocol):
    """Application-owned implementation of one bounded tool."""

    async def execute(
        self,
        context: ToolExecutionContext,
        payload: BaseModel,
    ) -> object:
        """Execute validated input using only trusted context."""
        ...


ToolAuthorizer = Callable[
    [ToolExecutionContext, SkillDefinition, ToolDefinition], Awaitable[bool]
]
AuditSink = Callable[["ToolExecutionAuditEvent"], Awaitable[None]]


class ToolExecutionAuditEvent(BaseModel):
    """Sanitized audit record; never contains tool arguments or returned content."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    organization_id: UUID
    actor_id: UUID
    request_id: str = Field(min_length=1, max_length=200)
    skill_id: str
    skill_version: str
    tool_id: str
    tool_version: str
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    outcome: str
    failure_code: str | None = None
    duration_ms: int = Field(ge=0)


class SkillExecutionError(RuntimeError):
    """Sanitized failure at the skill/tool execution boundary."""


class ToolNotAllowedError(SkillExecutionError):
    """The requested exact tool version is not declared by the skill."""


class ToolAuthorizationError(SkillExecutionError):
    """Server-side authorization denied the requested tool execution."""


class ToolExecutionTimeoutError(SkillExecutionError):
    """A tool exceeded its configured execution budget."""


class ToolAuditError(SkillExecutionError):
    """The required audit sink could not persist the execution record."""


def _payload_digest(payload: BaseModel) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class SkillToolExecutor:
    """Execute only registered tools declared by an exact skill version.

    Handlers, authorization and audit persistence are injected by trusted
    application composition. No dynamic imports, shell, filesystem or network
    capabilities are exposed by this runtime.
    """

    def __init__(
        self,
        *,
        registry: SkillRegistry,
        handlers: Mapping[tuple[str, str], ToolHandler],
        authorize: ToolAuthorizer,
        audit: AuditSink,
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 600:
            raise ValueError("Tool timeout must be greater than 0 and at most 600 seconds.")
        self._registry = registry
        self._handlers = dict(handlers)
        self._authorize = authorize
        self._audit = audit
        self._timeout_seconds = timeout_seconds

    def validate_skill_input(
        self,
        skill_id: str,
        skill_version: str,
        payload: object,
    ) -> BaseModel:
        """Resolve and validate input against one exact immutable skill version."""
        skill = self._registry.get_skill(skill_id, skill_version)
        return skill.validate_input(payload)

    def validate_skill_output(
        self,
        skill_id: str,
        skill_version: str,
        payload: object,
    ) -> BaseModel:
        """Resolve and validate output against one exact immutable skill version."""
        skill = self._registry.get_skill(skill_id, skill_version)
        return skill.validate_output(payload)

    async def execute_tool(
        self,
        *,
        context: ToolExecutionContext,
        skill_id: str,
        skill_version: str,
        tool_id: str,
        tool_version: str,
        arguments: object,
    ) -> BaseModel:
        """Execute a single allowlisted tool call and record a sanitized audit event."""
        skill = self._registry.get_skill(skill_id, skill_version)
        tool_reference = f"{tool_id}@{tool_version}"
        if tool_reference not in skill.allowed_tools:
            raise ToolNotAllowedError("Tool is not declared by this skill version.")

        tool = self._registry.get_tool(tool_id, tool_version)
        handler = self._handlers.get((tool_id, tool_version))
        if handler is None:
            raise SkillExecutionError("No application handler is installed for this tool.")

        validated_input = tool.validate_input(arguments)
        input_digest = _payload_digest(validated_input)
        started = time.perf_counter()
        outcome = "failure"
        failure_code: str | None = "tool_execution_failed"
        output_digest: str | None = None

        try:
            authorized = await asyncio.wait_for(
                self._authorize(context, skill, tool),
                timeout=self._timeout_seconds,
            )
            if authorized is not True:
                failure_code = "authorization_denied"
                raise ToolAuthorizationError("Server-side tool authorization was denied.")

            raw_output = await asyncio.wait_for(
                handler.execute(context, validated_input),
                timeout=self._timeout_seconds,
            )
            validated_output = tool.validate_output(raw_output)
            output_digest = _payload_digest(validated_output)
            outcome = "success"
            failure_code = None
            return validated_output
        except ToolAuthorizationError:
            raise
        except TimeoutError as exc:
            failure_code = "tool_timeout"
            raise ToolExecutionTimeoutError("Tool execution exceeded its time budget.") from exc
        except Exception as exc:
            failure_code = "tool_execution_failed"
            raise SkillExecutionError("Tool execution failed or returned invalid output.") from exc
        finally:
            event = ToolExecutionAuditEvent(
                organization_id=context.organization_id,
                actor_id=context.actor_id,
                request_id=context.request_id,
                skill_id=skill.skill_id,
                skill_version=skill.version,
                tool_id=tool.tool_id,
                tool_version=tool.version,
                input_sha256=input_digest,
                output_sha256=output_digest,
                outcome=outcome,
                failure_code=failure_code,
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            )
            try:
                await asyncio.wait_for(self._audit(event), timeout=self._timeout_seconds)
            except Exception as exc:
                raise ToolAuditError("Tool execution audit could not be persisted.") from exc
