import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.services.email import (
    RESEND_EMAILS_URL,
    EmailDeliveryError,
    EmailMessage,
    ResendEmailSender,
    get_email_sender,
)


def test_resend_sender_posts_email_and_idempotency_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "email_123"})

    sender = ResendEmailSender(
        api_key="re_test_key",
        from_address="onboarding@resend.dev",
        reply_to="support@example.com",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        sender.send_email(
            EmailMessage(
                to_email="recipient@example.com",
                subject="Welcome",
                text_body="Welcome to Aurora Studio.",
                idempotency_key="email-outbox:123",
            )
        )
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == RESEND_EMAILS_URL
    assert request.headers["Authorization"] == "Bearer re_test_key"
    assert request.headers["Idempotency-Key"] == "email-outbox:123"
    assert request.headers["Content-Type"] == "application/json"
    assert request.read() == (
        b'{"from":"onboarding@resend.dev","to":["recipient@example.com"],'
        b'"subject":"Welcome","text":"Welcome to Aurora Studio.",'
        b'"reply_to":"support@example.com"}'
    )


def test_resend_sender_omits_optional_headers_and_reply_to() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "email_123"})

    sender = ResendEmailSender(
        api_key="re_test_key",
        from_address="onboarding@resend.dev",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        sender.send_email(
            EmailMessage(
                to_email="recipient@example.com",
                subject="Welcome",
                text_body="Hello.",
            )
        )
    )

    assert "Idempotency-Key" not in captured[0].headers
    assert captured[0].read() == (
        b'{"from":"onboarding@resend.dev","to":["recipient@example.com"],'
        b'"subject":"Welcome","text":"Hello."}'
    )


def test_resend_sender_classifies_provider_error_without_leaking_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "sensitive provider detail"})

    sender = ResendEmailSender(
        api_key="re_test_key",
        from_address="onboarding@resend.dev",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EmailDeliveryError) as exc_info:
        asyncio.run(
            sender.send_email(
                EmailMessage(
                    to_email="recipient@example.com",
                    subject="Welcome",
                    text_body="Hello.",
                )
            )
        )

    assert exc_info.value.retryable is True
    assert exc_info.value.error_code == "provider_http_429"
    assert "sensitive provider detail" not in str(exc_info.value)


def test_provider_mode_builds_resend_sender() -> None:
    settings = Settings(
        _env_file=None,
        invitation_email_mode="provider",
        resend_api_key="re_test_key",
        email_from_address="onboarding@resend.dev",
    )

    sender = get_email_sender(settings)

    assert isinstance(sender, ResendEmailSender)


def test_provider_mode_requires_credentials_at_sender_creation() -> None:
    settings = Settings(
        _env_file=None,
        invitation_email_mode="provider",
    )

    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        get_email_sender(settings)


def test_resend_sender_rejects_empty_credentials() -> None:
    with pytest.raises(ValueError, match="API key cannot be empty"):
        ResendEmailSender(
            api_key=" ",
            from_address="onboarding@resend.dev",
        )
