"""Durable persistence adapters for permissioned skill execution."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tool_execution_audit import ToolExecutionAuditRecord
from app.skills.execution import AuditSink, ToolExecutionAuditEvent


def database_tool_audit_sink(
    session_factory: async_sessionmaker[AsyncSession],
) -> AuditSink:
    """Build an audit callback that commits sanitized events before returning.

    The executor awaits this callback before reporting a tool result. A failure
    propagates to the executor, which fails closed with ToolAuditError.
    """

    async def persist(event: ToolExecutionAuditEvent) -> None:
        if not isinstance(event, ToolExecutionAuditEvent):
            raise TypeError("Only validated tool execution audit events may be persisted.")
        async with session_factory() as session:
            async with session.begin():
                session.add(ToolExecutionAuditRecord(**event.model_dump(mode="python")))

    return persist
