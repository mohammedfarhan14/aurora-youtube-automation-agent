import asyncio

import httpx
import pytest

from app.services.email import EmailDeliveryError, EmailMessage, ResendEmailSender


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [
        (302, False),
        (400, False),
        (401, False),
        (403, False),
        (408, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_resend_classifies_http_failures(status_code, retryable):
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, request=request)

        sender = ResendEmailSender(
            api_key="test-api-key",
            from_address="noreply@example.com",
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(EmailDeliveryError) as captured:
            await sender.send_email(
                EmailMessage(
                    to_email="recipient@example.com",
                    subject="Test",
                    text_body="Test body",
                )
            )

        assert captured.value.retryable is retryable
        assert captured.value.error_code == f"provider_http_{status_code}"
        assert "test-api-key" not in str(captured.value)

    asyncio.run(scenario())


def test_resend_classifies_network_failure_as_retryable():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        sender = ResendEmailSender(
            api_key="test-api-key",
            from_address="noreply@example.com",
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(EmailDeliveryError) as captured:
            await sender.send_email(
                EmailMessage(
                    to_email="recipient@example.com",
                    subject="Test",
                    text_body="Test body",
                )
            )

        assert captured.value.retryable is True
        assert captured.value.error_code == "provider_network_error"
        assert "connection refused" not in str(captured.value)

    asyncio.run(scenario())
