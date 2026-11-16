import argparse
import asyncio
from uuid import UUID

from sqlalchemy import func, select

from app.core.config import get_settings
from app.database.engine import dispose_engine, get_session_factory
from app.models.email_outbox import EmailOutbox
from app.models.youtube import YouTubeChannel
from app.services.email_outbox_crypto import (
    decrypt_payload,
    encrypt_payload,
)
from app.services.email_outbox_crypto import (
    get_active_encryption_key as get_active_email_key,
)
from app.services.email_outbox_crypto import (
    get_encryption_keys as get_email_keys,
)
from app.services.youtube_crypto import (
    decrypt_refresh_token,
    encrypt_refresh_token,
)
from app.services.youtube_crypto import (
    get_active_encryption_key as get_active_youtube_key,
)
from app.services.youtube_crypto import (
    get_encryption_keys as get_youtube_keys,
)


def rotate_email_outbox_record(
    row, keys: dict[int, str], active_version: int, active_key: str
) -> bool:
    """Re-encrypt one outbox row; return False when it is already current."""
    if row.key_version == active_version:
        return False
    plaintext = decrypt_payload(
        row.encrypted_payload,
        keys,
        expected_key_version=row.key_version,
    )
    row.encrypted_payload = encrypt_payload(
        plaintext,
        active_key,
        key_version=active_version,
    )
    row.key_version = active_version
    return True


def rotate_youtube_channel_record(
    row, keys: dict[int, str], active_version: int, active_key: str
) -> bool:
    """Re-encrypt one YouTube credential; return False when it is already current."""
    if not row.encrypted_refresh_token or row.key_version == active_version:
        return False
    plaintext = decrypt_refresh_token(
        row.encrypted_refresh_token,
        keys,
        row.organization_id,
        row.channel_id,
        row.key_version,
    )
    row.encrypted_refresh_token = encrypt_refresh_token(
        plaintext,
        active_key,
        row.organization_id,
        row.channel_id,
        active_version,
    )
    row.key_version = active_version
    return True


async def rotate_email_outbox_batch(settings, batch_size: int = 100) -> int:
    """Re-encrypt one bounded, atomic batch of outbox payloads."""
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("Batch size must be between 1 and 1000.")
    keys = get_email_keys(settings)
    active_version, active_key = get_active_email_key(settings)
    factory = get_session_factory()
    rotated = 0

    async with factory() as session:
        async with session.begin():
            result = await session.scalars(
                select(EmailOutbox)
                .where(EmailOutbox.key_version != active_version)
                .order_by(EmailOutbox.created_at, EmailOutbox.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = list(result.all())
            for row in rows:
                rotated += int(rotate_email_outbox_record(row, keys, active_version, active_key))

    return rotated


async def rotate_youtube_tokens_batch(
    settings,
    batch_size: int = 100,
    *,
    organization_id: UUID | None = None,
) -> int:
    """Re-encrypt one bounded, atomic batch of stored YouTube refresh tokens.

    The optional organization scope supports safe tenant-targeted maintenance
    and makes batch behavior independently verifiable for each workspace.
    """
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("Batch size must be between 1 and 1000.")
    keys = get_youtube_keys(settings)
    active_version, active_key = get_active_youtube_key(settings)
    factory = get_session_factory()
    rotated = 0

    async with factory() as session:
        async with session.begin():
            statement = select(YouTubeChannel).where(
                YouTubeChannel.encrypted_refresh_token != "",
                YouTubeChannel.key_version != active_version,
            )
            if organization_id is not None:
                statement = statement.where(YouTubeChannel.organization_id == organization_id)
            result = await session.scalars(
                statement.order_by(YouTubeChannel.connected_at, YouTubeChannel.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = list(result.all())
            for row in rows:
                rotated += int(rotate_youtube_channel_record(row, keys, active_version, active_key))

    return rotated


async def _count_remaining(domain: str, active_version: int) -> int:
    model = EmailOutbox if domain == "email" else YouTubeChannel
    statement = select(func.count()).select_from(model).where(model.key_version != active_version)
    if domain == "youtube":
        statement = statement.where(YouTubeChannel.encrypted_refresh_token != "")
    async with get_session_factory()() as session:
        return int((await session.scalar(statement)) or 0)


async def _rotate_domain(domain: str, batch_size: int, max_batches: int) -> int:
    rotate = rotate_email_outbox_batch if domain == "email" else rotate_youtube_tokens_batch
    settings = get_settings()
    total = 0
    for _ in range(max_batches):
        count = await rotate(settings, batch_size)
        total += count
        if count == 0:
            active_version = (
                settings.email_outbox_active_key_version
                if domain == "email"
                else settings.integration_token_active_key_version
            )
            remaining = await _count_remaining(domain, active_version)
            if remaining:
                raise RuntimeError(
                    f"{domain} rotation paused with {remaining} record(s) still "
                    "using old key versions; rerun after transient locks clear."
                )
            return total
    active_version = (
        settings.email_outbox_active_key_version
        if domain == "email"
        else settings.integration_token_active_key_version
    )
    remaining = await _count_remaining(domain, active_version)
    if remaining == 0:
        return total
    raise RuntimeError(
        f"{domain} rotation reached the batch limit after {total} records; "
        f"{remaining} record(s) remain. Rerun the command to continue safely."
    )


async def _run(domain: str, batch_size: int, max_batches: int) -> None:
    try:
        domains = ("email", "youtube") if domain == "all" else (domain,)
        for selected in domains:
            total = await _rotate_domain(selected, batch_size, max_batches)
            print(f"{selected}: rotated {total} record(s).")
    finally:
        await dispose_engine()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rotate encrypted Aurora Studio records using configured keyrings."
    )
    parser.add_argument(
        "--domain",
        choices=("email", "youtube", "all"),
        default="email",
        help=(
            "Encryption domain to rotate (defaults to email; "
            "use all only when both keyrings are configured)."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Maximum records per database transaction (1-1000).",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=100,
        help="Maximum transactions per domain before a safe restart is required.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 1000:
        parser.error("--batch-size must be between 1 and 1000.")
    if args.max_batches < 1:
        parser.error("--max-batches must be positive.")
    asyncio.run(_run(args.domain, args.batch_size, args.max_batches))


if __name__ == "__main__":
    main()
