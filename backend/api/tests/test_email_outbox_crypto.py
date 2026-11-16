from types import SimpleNamespace

import pytest

from app.services.email_outbox_crypto import (
    decrypt_payload,
    encrypt_payload,
)
from app.services.encryption_key_rotation import rotate_email_outbox_record

TEST_KEY = "a" * 64
ALTERNATE_KEY = "b" * 64


def test_encrypt_and_decrypt_payload() -> None:
    plaintext = "Verification token: sensitive-value"

    encrypted = encrypt_payload(plaintext, TEST_KEY)

    decrypted = decrypt_payload(encrypted, {1: TEST_KEY})

    assert decrypted == plaintext
    assert plaintext not in encrypted


def test_encryption_uses_a_fresh_nonce() -> None:
    plaintext = "Same message"

    first = encrypt_payload(plaintext, TEST_KEY)
    second = encrypt_payload(plaintext, TEST_KEY)

    assert first != second


def test_decryption_rejects_modified_ciphertext() -> None:
    encrypted = encrypt_payload("Sensitive data", TEST_KEY)
    version, nonce, ciphertext = encrypted.split(":")

    modified_ciphertext = (
        "A" if ciphertext[0] != "A" else "B"
    ) + ciphertext[1:]

    modified_payload = ":".join(
        (version, nonce, modified_ciphertext)
    )

    with pytest.raises(ValueError, match="authentication failed"):
        decrypt_payload(modified_payload, {1: TEST_KEY})


def test_decryption_uses_the_payload_key_version() -> None:
    encrypted = encrypt_payload(
        "Versioned message",
        ALTERNATE_KEY,
        key_version=2,
    )

    assert decrypt_payload(
        encrypted,
        {
            1: TEST_KEY,
            2: ALTERNATE_KEY,
        },
    ) == "Versioned message"


def test_decryption_rejects_missing_key_version() -> None:
    encrypted = encrypt_payload(
        "Versioned message",
        TEST_KEY,
        key_version=1,
    )

    with pytest.raises(ValueError, match="version is unavailable"):
        decrypt_payload(encrypted, {2: ALTERNATE_KEY})


def test_encryption_rejects_invalid_key_length() -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        encrypt_payload("Sensitive data", "abcd")


def test_encryption_rejects_invalid_hexadecimal_key() -> None:
    with pytest.raises(ValueError, match="valid hexadecimal"):
        encrypt_payload("Sensitive data", "z" * 64)


def test_decryption_rejects_invalid_payload_format() -> None:
    with pytest.raises(ValueError, match="payload format"):
        decrypt_payload("invalid-payload", {1: TEST_KEY})

def test_decryption_accepts_matching_expected_key_version() -> None:
    encrypted = encrypt_payload(
        "Versioned message",
        TEST_KEY,
        key_version=1,
    )

    assert decrypt_payload(
        encrypted,
        {1: TEST_KEY},
        expected_key_version=1,
    ) == "Versioned message"


def test_decryption_rejects_mismatched_expected_key_version() -> None:
    encrypted = encrypt_payload(
        "Versioned message",
        TEST_KEY,
        key_version=1,
    )

    with pytest.raises(
        ValueError,
        match="does not match outbox record",
    ):
        decrypt_payload(
            encrypted,
            {1: TEST_KEY},
            expected_key_version=2,
        )


def test_decryption_remains_compatible_without_expected_key_version() -> None:
    encrypted = encrypt_payload("Legacy-compatible message", TEST_KEY)

    assert decrypt_payload(encrypted, {1: TEST_KEY}) == (
        "Legacy-compatible message"
    )


def test_email_outbox_record_rotation_is_safe_and_idempotent() -> None:
    old_key = "a" * 64
    new_key = "b" * 64
    plaintext = "Sensitive verification URL"
    row = SimpleNamespace(
        key_version=1,
        encrypted_payload=encrypt_payload(plaintext, old_key, key_version=1),
    )
    original_ciphertext = row.encrypted_payload

    assert rotate_email_outbox_record(row, {1: old_key, 2: new_key}, 2, new_key)
    assert row.key_version == 2
    assert row.encrypted_payload != original_ciphertext
    assert decrypt_payload(row.encrypted_payload, {1: old_key, 2: new_key}) == plaintext
    assert not rotate_email_outbox_record(row, {1: old_key, 2: new_key}, 2, new_key)


def test_email_outbox_rotation_fails_closed_when_old_key_is_missing() -> None:
    row = SimpleNamespace(
        key_version=1,
        encrypted_payload=encrypt_payload("private", TEST_KEY, key_version=1),
    )
    original = row.encrypted_payload

    with pytest.raises(ValueError, match="version is unavailable"):
        rotate_email_outbox_record(row, {2: ALTERNATE_KEY}, 2, ALTERNATE_KEY)

    assert row.key_version == 1
    assert row.encrypted_payload == original
