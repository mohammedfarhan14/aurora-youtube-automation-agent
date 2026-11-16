import asyncio
from functools import wraps
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.skills import SkillDefinition, SkillRegistry, ToolDefinition
from app.skills.execution import (
    SkillExecutionError,
    SkillToolExecutor,
    ToolAuditError,
    ToolAuthorizationError,
    ToolExecutionAuditEvent,
    ToolExecutionContext,
    ToolExecutionTimeoutError,
    ToolNotAllowedError,
)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        asyncio.run(function(*args, **kwargs))

    return wrapper


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=200)


class SearchOutput(StrictModel):
    source_ids: list[str]


class SkillInput(StrictModel):
    topic: str = Field(min_length=1, max_length=200)


class SkillOutput(StrictModel):
    summary: str


class SearchHandler:
    def __init__(self, output=None, delay=0):
        self.output = output or {"source_ids": ["source-1"]}
        self.delay = delay
        self.context = None
        self.payload = None

    async def execute(self, context, payload):
        self.context = context
        self.payload = payload
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.output


def build_executor(*, handler=None, authorized=True, audit_failure=False, timeout=0.1):
    tool = ToolDefinition(
        tool_id="research.search",
        version="1.0.0",
        description="Search the approved research corpus.",
        input_schema=SearchInput,
        output_schema=SearchOutput,
    )
    skill = SkillDefinition(
        skill_id="research.topic_discovery",
        version="1.0.0",
        description="Find candidate topics from approved sources.",
        input_schema=SkillInput,
        output_schema=SkillOutput,
        allowed_tools=frozenset({"research.search@1.0.0"}),
    )
    registry = SkillRegistry(tools=[tool], skills=[skill])
    events = []
    handler = handler or SearchHandler()

    async def authorize(context, requested_skill, requested_tool):
        return authorized

    async def audit(event):
        if audit_failure:
            raise RuntimeError("database details must not escape")
        events.append(event)

    executor = SkillToolExecutor(
        registry=registry,
        handlers={("research.search", "1.0.0"): handler},
        authorize=authorize,
        audit=audit,
        timeout_seconds=timeout,
    )
    context = ToolExecutionContext(
        organization_id=uuid4(),
        actor_id=uuid4(),
        request_id="request-123",
        idempotency_key="run-123:research.search",
    )
    return executor, handler, events, context


@async_test
async def test_executor_runs_only_registered_allowlisted_tool_and_audits_hashes():
    executor, handler, events, context = build_executor()

    result = await executor.execute_tool(
        context=context,
        skill_id="research.topic_discovery",
        skill_version="1.0.0",
        tool_id="research.search",
        tool_version="1.0.0",
        arguments={"query": "solar energy"},
    )

    assert result.source_ids == ["source-1"]
    assert handler.context is context
    assert handler.payload.query == "solar energy"
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ToolExecutionAuditEvent)
    assert event.organization_id == context.organization_id
    assert event.actor_id == context.actor_id
    assert event.outcome == "success"
    assert event.failure_code is None
    assert len(event.input_sha256) == 64
    assert len(event.output_sha256) == 64
    assert "solar energy" not in event.model_dump_json()


@async_test
async def test_executor_denies_tool_when_server_authorization_fails():
    executor, handler, events, context = build_executor(authorized=False)

    with pytest.raises(ToolAuthorizationError):
        await executor.execute_tool(
            context=context,
            skill_id="research.topic_discovery",
            skill_version="1.0.0",
            tool_id="research.search",
            tool_version="1.0.0",
            arguments={"query": "solar energy"},
        )

    assert handler.context is None
    assert len(events) == 1
    assert events[0].failure_code == "authorization_denied"
    assert events[0].outcome == "failure"


@async_test
async def test_executor_rejects_tool_not_declared_by_exact_skill_version():
    executor, handler, events, context = build_executor()

    with pytest.raises(ToolNotAllowedError):
        await executor.execute_tool(
            context=context,
            skill_id="research.topic_discovery",
            skill_version="1.0.0",
            tool_id="filesystem.read",
            tool_version="1.0.0",
            arguments={"query": "x"},
        )

    assert handler.context is None
    assert events == []


@async_test
async def test_executor_rejects_unknown_arguments_before_invoking_handler():
    executor, handler, events, context = build_executor()

    with pytest.raises(ValidationError):
        await executor.execute_tool(
            context=context,
            skill_id="research.topic_discovery",
            skill_version="1.0.0",
            tool_id="research.search",
            tool_version="1.0.0",
            arguments={"query": "solar energy", "shell": "echo unsafe"},
        )

    assert handler.context is None
    assert events == []


@async_test
async def test_executor_rejects_invalid_handler_output_and_records_failure():
    handler = SearchHandler(output={"source_ids": "not-a-list"})
    executor, _, events, context = build_executor(handler=handler)

    with pytest.raises(SkillExecutionError):
        await executor.execute_tool(
            context=context,
            skill_id="research.topic_discovery",
            skill_version="1.0.0",
            tool_id="research.search",
            tool_version="1.0.0",
            arguments={"query": "solar energy"},
        )

    assert len(events) == 1
    assert events[0].failure_code == "tool_execution_failed"
    assert events[0].output_sha256 is None


@async_test
async def test_executor_enforces_tool_timeout_and_records_failure():
    handler = SearchHandler(delay=0.2)
    executor, _, events, context = build_executor(handler=handler, timeout=0.01)

    with pytest.raises(ToolExecutionTimeoutError):
        await executor.execute_tool(
            context=context,
            skill_id="research.topic_discovery",
            skill_version="1.0.0",
            tool_id="research.search",
            tool_version="1.0.0",
            arguments={"query": "solar energy"},
        )

    assert len(events) == 1
    assert events[0].failure_code == "tool_timeout"


@async_test
async def test_executor_surfaces_audit_persistence_failure_after_handler_runs():
    executor, handler, _, context = build_executor(audit_failure=True)

    with pytest.raises(ToolAuditError):
        await executor.execute_tool(
            context=context,
            skill_id="research.topic_discovery",
            skill_version="1.0.0",
            tool_id="research.search",
            tool_version="1.0.0",
            arguments={"query": "solar energy"},
        )

    # A post-execution audit failure cannot roll back a completed tool action.
    assert handler.context is context


def test_executor_validates_skill_inputs_and_outputs_against_exact_version():
    executor, _, _, _ = build_executor()

    assert executor.validate_skill_input(
        "research.topic_discovery", "1.0.0", {"topic": "Solar energy"}
    ).topic == "Solar energy"
    assert executor.validate_skill_output(
        "research.topic_discovery", "1.0.0", {"summary": "A validated summary"}
    ).summary == "A validated summary"

    with pytest.raises(ValidationError):
        executor.validate_skill_input(
            "research.topic_discovery",
            "1.0.0",
            {"topic": "Solar energy", "tool": "filesystem.read"},
        )
