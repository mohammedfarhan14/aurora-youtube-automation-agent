from types import SimpleNamespace

import pytest

from app.services.encryption_key_rotation import rotate_youtube_channel_record
from app.services.youtube_crypto import (
    decrypt_refresh_token,
    decrypt_upload_session_url,
    encrypt_refresh_token,
    encrypt_upload_session_url,
)


def test_refresh_token_round_trip_and_context_binding():
    from uuid import uuid4

    organization_id = uuid4()
    channel_id = "UC123456789"
    key = "ab" * 32
    encrypted = encrypt_refresh_token(
        "refresh-secret", key, organization_id, channel_id
    )

    assert decrypt_refresh_token(
        encrypted, {1: key}, organization_id, channel_id, 1
    ) == "refresh-secret"

    with pytest.raises(ValueError, match="authentication failed"):
        decrypt_refresh_token(
            encrypted, {1: key}, uuid4(), channel_id, 1
        )


def test_refresh_token_rejects_tampering():
    from uuid import uuid4

    organization_id = uuid4()
    channel_id = "UC123456789"
    key = "cd" * 32
    encrypted = encrypt_refresh_token("refresh-secret", key, organization_id, channel_id)
    version, nonce, ciphertext = encrypted.split(":")
    modified = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    tampered = f"{version}:{nonce}:{modified}"

    with pytest.raises(ValueError):
        decrypt_refresh_token(
            tampered, {1: key}, organization_id, channel_id, 1
        )


def test_refresh_token_rejects_wrong_key_version():
    from uuid import uuid4

    with pytest.raises(ValueError, match="unavailable or mismatched"):
        decrypt_refresh_token(
            "2:bm9uY2U:Y2lwaGVydGV4dA", {1: "ef" * 32}, uuid4(), "UC1", 1
        )


def test_youtube_channel_rotation_preserves_context_and_is_idempotent():
    from uuid import uuid4

    organization_id = uuid4()
    channel_id = "UC-rotation-test"
    old_key = "ab" * 32
    new_key = "cd" * 32
    plaintext = "youtube-refresh-secret"
    row = SimpleNamespace(
        organization_id=organization_id,
        channel_id=channel_id,
        key_version=1,
        encrypted_refresh_token=encrypt_refresh_token(
            plaintext, old_key, organization_id, channel_id, key_version=1
        ),
    )
    original = row.encrypted_refresh_token

    assert rotate_youtube_channel_record(row, {1: old_key, 2: new_key}, 2, new_key)
    assert row.key_version == 2
    assert row.encrypted_refresh_token != original
    assert decrypt_refresh_token(
        row.encrypted_refresh_token, {1: old_key, 2: new_key},
        organization_id, channel_id, 2
    ) == plaintext
    assert not rotate_youtube_channel_record(row, {1: old_key, 2: new_key}, 2, new_key)


def test_youtube_rotation_fails_closed_when_old_key_is_missing():
    from uuid import uuid4

    organization_id = uuid4()
    row = SimpleNamespace(
        organization_id=organization_id,
        channel_id="UC-rotation-test",
        key_version=1,
        encrypted_refresh_token=encrypt_refresh_token(
            "private", "ab" * 32, organization_id, "UC-rotation-test"
        ),
    )
    original = row.encrypted_refresh_token

    with pytest.raises(ValueError, match="unavailable or mismatched"):
        rotate_youtube_channel_record(row, {2: "cd" * 32}, 2, "cd" * 32)

    assert row.key_version == 1
    assert row.encrypted_refresh_token == original


def test_upload_session_url_round_trip_and_job_tenant_binding():
    from uuid import uuid4

    organization_id = uuid4()
    job_id = uuid4()
    session_url = (
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?upload_id=opaque-session-secret"
    )
    key = "ab" * 32
    encrypted = encrypt_upload_session_url(
        session_url,
        key,
        organization_id,
        job_id,
        key_version=1,
    )

    assert decrypt_upload_session_url(
        encrypted,
        {1: key},
        organization_id,
        job_id,
        expected_key_version=1,
    ) == session_url

    with pytest.raises(ValueError, match="authentication failed"):
        decrypt_upload_session_url(
            encrypted,
            {1: key},
            uuid4(),
            job_id,
            expected_key_version=1,
        )

    with pytest.raises(ValueError, match="authentication failed"):
        decrypt_upload_session_url(
            encrypted,
            {1: key},
            organization_id,
            uuid4(),
            expected_key_version=1,
        )


def test_upload_session_url_rejects_untrusted_hosts_and_tampering():
    from uuid import uuid4

    organization_id = uuid4()
    job_id = uuid4()
    key = "cd" * 32
    with pytest.raises(ValueError, match="approved Google upload URL"):
        encrypt_upload_session_url(
            "https://attacker.example/upload/youtube/v3/videos?upload_id=secret",
            key,
            organization_id,
            job_id,
        )

    encrypted = encrypt_upload_session_url(
        "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=secret",
        key,
        organization_id,
        job_id,
    )
    version, nonce, ciphertext = encrypted.split(":")
    modified = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    with pytest.raises(ValueError):
        decrypt_upload_session_url(
            f"{version}:{nonce}:{modified}",
            {1: key},
            organization_id,
            job_id,
            expected_key_version=1,
        )
