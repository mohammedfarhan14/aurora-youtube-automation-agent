"""Pluggable operator-alert delivery using the existing encrypted email outbox."""

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models.organization import Organization
from app.models.user import User
from app.services.email import EmailMessage
from app.services.email_outbox import enqueue_email_message

logger = logging.getLogger(__name__)

OperationsAlertSink = Callable[
    [async_sessionmaker[AsyncSession], UUID, str, str, Settings],
    Awaitable[bool],
]


async def _enqueue_email_alert(
    session_factory: async_sessionmaker[AsyncSession],
    organization_id: UUID,
    subject: str,
    body: str,
    settings: Settings,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            organization = await session.get(Organization, organization_id)
            if organization is None:
                return False
            recipient = await session.get(User, organization.created_by_user_id)
            if recipient is None or not recipient.email:
                return False
            await enqueue_email_message(
                session,
                EmailMessage(
                    to_email=recipient.email,
                    subject=subject[:255],
                    text_body=body[:20_000],
                ),
                message_type="operations_alert",
                user_id=recipient.id,
                settings=settings,
            )
    return True


_ALERT_SINKS: dict[str, OperationsAlertSink] = {
    "email": _enqueue_email_alert,
}


def register_operations_alert_sink(
    channel: str,
    sink: OperationsAlertSink,
) -> None:
    """Register an additional delivery channel without changing worker logic."""
    key = channel.strip().lower()
    if not key or key in _ALERT_SINKS:
        raise ValueError("Operations alert channel is empty or already registered.")
    _ALERT_SINKS[key] = sink


async def enqueue_operations_alert(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    organization_id: UUID,
    subject: str,
    body: str,
    settings: Settings | None = None,
) -> bool:
    """Queue one alert after a durable terminal-state transition.

    The selected sink owns delivery semantics. The email sink uses the
    encrypted outbox, whose worker owns retries. Alert enqueue failures never
    mask the original workflow failure.
    """
    settings = settings or get_settings()
    if not settings.operations_alerts_enabled:
        return False
    sink = _ALERT_SINKS.get(settings.operations_alert_channel.strip().lower())
    if sink is None:
        logger.error(
            "Unsupported operations alert channel: channel=%s",
            settings.operations_alert_channel,
        )
        return False
    try:
        return await sink(
            session_factory,
            organization_id,
            subject,
            body,
            settings,
        )
    except Exception as exc:
        logger.warning(
            "Could not enqueue operations alert: organization_id=%s error_type=%s",
            organization_id,
            type(exc).__name__,
        )
        return False
