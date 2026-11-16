import pytest

from app.core.passwords import (
    hash_password,
    password_needs_rehash,
    verify_password,
)


def test_password_is_hashed():
    password = "Aurora-Test-Password-2026"

    password_hash = hash_password(password)

    assert verify_password(password, password_hash) is True


def test_incorrect_password_fails():
    password_hash = hash_password("Correct-Password-123")

    assert verify_password("Incorrect-Password-456", password_hash) is False


def test_invalid_hash_fails_verification():
    assert verify_password("Some-Password", "Invalid-Hash") is False


def test_empty_password_is_rejected():
    with pytest.raises(ValueError):
        hash_password("")


def test_current_hash_does_not_need_rehash():
    password_hash = hash_password("Aurora-Test-Password-2026")

    assert password_needs_rehash(password_hash) is False


def test_correct_password_verifies():
    password = "Aurora-Test-Password-2026"
    password_hash = hash_password(password)

    assert verify_password(password, password_hash) is True
