import re

from app.core.tokens import generate_invitation_token, hash_invitation_token


def test_generate_invitation_token_returns_url_safe_token():
    token = generate_invitation_token()

    assert isinstance(token, str)
    assert len(token) == 64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)


def test_generate_invitation_token_returns_unique_values():
    first_token = generate_invitation_token()
    second_token = generate_invitation_token()

    assert first_token != second_token


def test_hash_invitation_token_returns_sha256_hex_digest():
    token_hash = hash_invitation_token("example-invitation-token")

    assert len(token_hash) == 64
    assert re.fullmatch(r"[a-f0-9]{64}", token_hash)


def test_hash_invitation_token_is_deterministic():
    token = generate_invitation_token()

    assert hash_invitation_token(token) == hash_invitation_token(token)


def test_raw_token_is_not_the_stored_hash():
    token = generate_invitation_token()
    token_hash = hash_invitation_token(token)

    assert token != token_hash
