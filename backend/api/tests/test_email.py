import asyncio
from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.services.email import (
    ConsoleEmailSender,
    ConsoleInvitationEmailSender,
    get_invitation_email_sender,
)


def test_development_uses_console_email_sender():
    settings = Settings(
        _env_file=None,
        app_env="development",
        invitation_email_mode="console",
    )

    sender = get_invitation_email_sender(settings)

    assert isinstance(sender, ConsoleInvitationEmailSender)
    assert isinstance(sender.sender, ConsoleEmailSender)


def test_console_email_sender_prints_invitation(capsys):
    # Explicitly inject the console adapter. Do not load the developer's .env.
    sender = ConsoleInvitationEmailSender(sender=ConsoleEmailSender())
    expires_at = datetime(2026, 11, 16, tzinfo=UTC)

    asyncio.run(
        sender.send_invitation(
            to_email="invitee@example.com",
            organization_name="Aurora Test",
            inviter_name="Test Owner",
            invitation_url="http://localhost:5173/accept-invitation?token=test-token",
            expires_at=expires_at,
        )
    )

    output = capsys.readouterr().out

    assert "invitee@example.com" in output
    assert "Aurora Test" in output
    assert "Test Owner" in output
    assert "test-token" in output
    assert expires_at.isoformat() in output


def test_console_delivery_is_rejected_outside_development():
    settings = Settings(
        _env_file=None,
        app_env="test",
        invitation_email_mode="console",
    )

    with pytest.raises(RuntimeError, match="only allowed in development"):
        get_invitation_email_sender(settings)


def test_unconfigured_provider_raises_error():
    settings = Settings(
        _env_file=None,
        app_env="development",
        invitation_email_mode="provider",
    )

    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        get_invitation_email_sender(settings)
