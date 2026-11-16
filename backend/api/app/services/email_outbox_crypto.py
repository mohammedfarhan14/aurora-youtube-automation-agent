import base64
import secrets
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LENGTH = 12
KEY_LENGTH = 32
CURRENT_KEY_VERSION = 1


def _decode_key(key_hex: str) -> bytes:
    try:
        key = bytes.fromhex(key_hex)
    except ValueError as exc:
        raise ValueError("Encryption key must be valid hexadecimal.") from exc

    if len(key) != KEY_LENGTH:
        raise ValueError("Encryption key must be exactly 32 bytes.")

    return key


def _encode_base64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_base64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)

    try:
        return base64.b64decode(
            data + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Invalid encrypted payload encoding.") from exc


def encrypt_payload(
    plaintext: str,
    encryption_key: str,
    key_version: int = CURRENT_KEY_VERSION,
) -> str:
    if not isinstance(plaintext, str):
        raise TypeError("Plaintext must be a string.")

    if key_version < 1:
        raise ValueError("Key version must be a positive integer.")

    key = _decode_key(encryption_key)
    nonce = secrets.token_bytes(NONCE_LENGTH)

    associated_data = f"aurora-email-outbox:v{key_version}".encode()

    ciphertext = AESGCM(key).encrypt(
        nonce,
        plaintext.encode("utf-8"),
        associated_data,
    )

    return ":".join(
        (
            str(key_version),
            _encode_base64(nonce),
            _encode_base64(ciphertext),
        )
    )


def decrypt_payload(
    encrypted_payload: str,
    encryption_keys: Mapping[int, str],
    *,
    expected_key_version: int | None = None,
) -> str:
    parts = encrypted_payload.split(":")

    if len(parts) != 3:
        raise ValueError("Invalid encrypted payload format.")

    version_text, nonce_text, ciphertext_text = parts

    try:
        key_version = int(version_text)
    except ValueError as exc:
        raise ValueError("Invalid encryption key version.") from exc

    if key_version < 1:
        raise ValueError("Invalid encryption key version.")

    if (
    expected_key_version is not None
    and key_version != expected_key_version
    ):
        raise ValueError("Encryption key version does not match outbox record.")

    encryption_key = encryption_keys.get(key_version)

    if encryption_key is None:
        raise ValueError("Encryption key version is unavailable.")

    key = _decode_key(encryption_key)
    nonce = _decode_base64(nonce_text)
    ciphertext = _decode_base64(ciphertext_text)

    if len(nonce) != NONCE_LENGTH:
        raise ValueError("Invalid encryption nonce.")

    associated_data = f"aurora-email-outbox:v{key_version}".encode()

    try:
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            associated_data,
        )
    except InvalidTag as exc:
        raise ValueError("Encrypted payload authentication failed.") from exc

    return plaintext.decode("utf-8")


def get_encryption_keys(settings) -> dict[int, str]:
    """Return all configured email-outbox keys indexed by persisted version."""
    from app.core.encryption_keys import parse_encryption_keyring

    return parse_encryption_keyring(
        settings.email_outbox_encryption_keyring,
        settings.email_outbox_encryption_key,
        domain="Email outbox",
    )


def get_active_encryption_key(settings) -> tuple[int, str]:
    """Return the active write-key version and its key material."""
    keys = get_encryption_keys(settings)
    version = settings.email_outbox_active_key_version
    key = keys.get(version)
    if key is None:
        raise RuntimeError(
            "Email outbox encryption key is not configured or active key "
            f"version {version} is unavailable."
        )
    return version, key
