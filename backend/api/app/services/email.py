from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from app.core.config import Settings, get_settings

RESEND_EMAILS_URL = "https://api.resend.com/emails"


class EmailDeliveryError(RuntimeError):
    """A sanitized provider failure with an explicit retry policy."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        error_code: str,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.error_code = error_code


@dataclass(frozen=True)
class EmailMessage:
    """A generic email message."""

    to_email: str
    subject: str
    text_body: str
    idempotency_key: str | None = None


class EmailSender(Protocol):
    """Interface implemented by email delivery providers."""

    async def send_email(self, message: EmailMessage) -> None:
        """Deliver an email message."""


class ConsoleEmailSender:
    """Development-only email adapter."""

    async def send_email(self, message: EmailMessage) -> None:
        print("\n========== AURORA STUDIO EMAIL ==========")
        print(f"To: {message.to_email}")
        print(f"Subject: {message.subject}")
        print(f"Body:\n{message.text_body}")
        print("=========================================\n")


class ResendEmailSender:
    """Resend REST API adapter with per-message idempotency support."""

    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
        reply_to: str | None = None,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Resend API key cannot be empty.")
        if not from_address.strip():
            raise ValueError("Email from address cannot be empty.")

        self._api_key = api_key
        self._from_address = from_address
        self._reply_to = reply_to.strip() if reply_to and reply_to.strip() else None
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def send_email(self, message: EmailMessage) -> None:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if message.idempotency_key:
            headers["Idempotency-Key"] = message.idempotency_key

        payload: dict[str, object] = {
            "from": self._from_address,
            "to": [message.to_email],
            "subject": message.subject,
            "text": message.text_body,
        }
        if self._reply_to:
            payload["reply_to"] = self._reply_to

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    RESEND_EMAILS_URL,
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            retryable = status_code in {408, 429} or status_code >= 500
            raise EmailDeliveryError(
                "Email provider rejected the delivery request.",
                retryable=retryable,
                error_code=f"provider_http_{status_code}",
            ) from exc
        except httpx.RequestError as exc:
            raise EmailDeliveryError(
                "Email provider could not be reached.",
                retryable=True,
                error_code="provider_network_error",
            ) from exc


def get_email_sender(
    settings: Settings | None = None,
) -> EmailSender:
    """Return the configured generic email adapter."""

    settings = settings or get_settings()
    environment = settings.app_env.strip().lower()

    if settings.invitation_email_mode == "console":
        if environment != "development":
            raise RuntimeError("Console email delivery is only allowed in development.")

        return ConsoleEmailSender()

    if (
        settings.resend_api_key is None
        or not settings.resend_api_key.get_secret_value().strip()
        or settings.email_from_address is None
        or not settings.email_from_address.strip()
    ):
        raise RuntimeError("Resend email configuration is incomplete.")

    return ResendEmailSender(
        api_key=settings.resend_api_key.get_secret_value(),
        from_address=settings.email_from_address,
        reply_to=settings.email_reply_to,
        timeout_seconds=settings.email_outbox_delivery_timeout_seconds,
    )


class InvitationEmailSender(Protocol):
    """Backward-compatible interface for invitation emails."""

    async def send_invitation(
        self,
        *,
        to_email: str,
        organization_name: str,
        inviter_name: str,
        invitation_url: str,
        expires_at: datetime,
    ) -> None:
        """Deliver an organization invitation."""


class ConsoleInvitationEmailSender:
    """Backward-compatible invitation adapter."""

    def __init__(self, sender: EmailSender | None = None) -> None:
        self.sender = sender or get_email_sender()

    async def send_invitation(
        self,
        *,
        to_email: str,
        organization_name: str,
        inviter_name: str,
        invitation_url: str,
        expires_at: datetime,
    ) -> None:
        message = EmailMessage(
            to_email=to_email,
            subject=f"Invitation to join {organization_name} on Aurora Studio",
            text_body=(
                f"{inviter_name} invited you to join {organization_name} "
                f"on Aurora Studio.\n\n"
                f"Accept your invitation: {invitation_url}\n\n"
                f"This invitation expires at {expires_at.isoformat()}."
            ),
        )

        await self.sender.send_email(message)


def get_invitation_email_sender(
    settings: Settings | None = None,
) -> InvitationEmailSender:
    """Return the backward-compatible invitation email adapter."""

    return ConsoleInvitationEmailSender(get_email_sender(settings))
