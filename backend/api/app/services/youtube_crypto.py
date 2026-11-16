import base64
import secrets
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LENGTH = 12
KEY_LENGTH = 32
CURRENT_KEY_VERSION = 1


def _decode_key(key_hex: str) -> bytes:
    try:
        key = bytes.fromhex(key_hex)
    except ValueError as exc:
        raise ValueError("Integration encryption key must be valid hexadecimal.") from exc
    if len(key) != KEY_LENGTH:
        raise ValueError("Integration encryption key must be exactly 32 bytes.")
    return key


def _aad(version: int, organization_id: UUID, channel_id: str) -> bytes:
    return f"aurora-youtube-refresh:v{version}:{organization_id}:{channel_id}".encode()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Invalid encrypted token encoding.") from exc


def encrypt_refresh_token(
    token: str, key_hex: str, organization_id: UUID, channel_id: str,
    key_version: int = CURRENT_KEY_VERSION,
) -> str:
    if not token:
        raise ValueError("Refresh token cannot be empty.")
    if key_version < 1:
        raise ValueError("Key version must be positive.")
    nonce = secrets.token_bytes(NONCE_LENGTH)
    ciphertext = AESGCM(_decode_key(key_hex)).encrypt(
        nonce, token.encode("utf-8"), _aad(key_version, organization_id, channel_id)
    )
    return ":".join((str(key_version), _b64encode(nonce), _b64encode(ciphertext)))


def decrypt_refresh_token(
    encrypted: str, keys: dict[int, str], organization_id: UUID, channel_id: str,
    expected_key_version: int,
) -> str:
    parts = encrypted.split(":")
    if len(parts) != 3:
        raise ValueError("Invalid encrypted token format.")
    try:
        version = int(parts[0])
    except ValueError as exc:
        raise ValueError("Invalid encryption key version.") from exc
    if version != expected_key_version or version not in keys:
        raise ValueError("Encryption key version is unavailable or mismatched.")
    nonce, ciphertext = _b64decode(parts[1]), _b64decode(parts[2])
    if len(nonce) != NONCE_LENGTH:
        raise ValueError("Invalid encryption nonce.")
    try:
        plaintext = AESGCM(_decode_key(keys[version])).decrypt(
            nonce, ciphertext, _aad(version, organization_id, channel_id)
        )
    except InvalidTag as exc:
        raise ValueError("Encrypted token authentication failed.") from exc
    return plaintext.decode("utf-8")


def get_encryption_keys(settings) -> dict[int, str]:
    """Return all configured YouTube integration keys indexed by version."""
    from app.core.encryption_keys import parse_encryption_keyring

    return parse_encryption_keyring(
        settings.integration_token_encryption_keyring,
        settings.integration_token_encryption_key,
        domain="Integration token",
    )


def get_active_encryption_key(settings) -> tuple[int, str]:
    """Return the active YouTube token write-key version and key material."""
    keys = get_encryption_keys(settings)
    version = settings.integration_token_active_key_version
    key = keys.get(version)
    if key is None:
        raise ValueError(
            "Integration token encryption key is not configured or active "
            f"version {version} is unavailable."
        )
    return version, key


def _upload_session_aad(version: int, organization_id: UUID, job_id: UUID) -> bytes:
    return (
        f"aurora-youtube-upload-session:v{version}:"
        f"{organization_id}:{job_id}"
    ).encode()


def _validate_upload_session_url(session_url: str) -> None:
    parsed = urlsplit(session_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid YouTube upload session URL port.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.googleapis.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/upload/youtube/v3/videos")
        or not parse_qs(parsed.query).get("upload_id")
        or parsed.fragment
    ):
        raise ValueError("YouTube upload session URL is not an approved Google upload URL.")


def encrypt_upload_session_url(
    session_url: str,
    key_hex: str,
    organization_id: UUID,
    job_id: UUID,
    key_version: int = CURRENT_KEY_VERSION,
) -> str:
    """Encrypt the resumable session URI with tenant/job-bound authenticated data."""
    _validate_upload_session_url(session_url)
    if key_version < 1:
        raise ValueError("Key version must be positive.")
    nonce = secrets.token_bytes(NONCE_LENGTH)
    ciphertext = AESGCM(_decode_key(key_hex)).encrypt(
        nonce,
        session_url.encode("utf-8"),
        _upload_session_aad(key_version, organization_id, job_id),
    )
    return ":".join((str(key_version), _b64encode(nonce), _b64encode(ciphertext)))


def decrypt_upload_session_url(
    encrypted: str,
    keys: dict[int, str],
    organization_id: UUID,
    job_id: UUID,
    expected_key_version: int,
) -> str:
    parts = encrypted.split(":")
    if len(parts) != 3:
        raise ValueError("Invalid encrypted upload session format.")
    try:
        version = int(parts[0])
    except ValueError as exc:
        raise ValueError("Invalid upload session encryption key version.") from exc
    if version != expected_key_version or version not in keys:
        raise ValueError("Upload session encryption key version is unavailable or mismatched.")
    nonce, ciphertext = _b64decode(parts[1]), _b64decode(parts[2])
    if len(nonce) != NONCE_LENGTH:
        raise ValueError("Invalid upload session encryption nonce.")
    try:
        plaintext = AESGCM(_decode_key(keys[version])).decrypt(
            nonce,
            ciphertext,
            _upload_session_aad(version, organization_id, job_id),
        )
    except InvalidTag as exc:
        raise ValueError("Encrypted upload session authentication failed.") from exc
    session_url = plaintext.decode("utf-8")
    _validate_upload_session_url(session_url)
    return session_url
