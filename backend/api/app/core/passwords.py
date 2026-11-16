from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

_password_hasher = PasswordHasher()
# Used to perform password verification for unknown accounts.
# Generated once per application process, not on every login attempt.
DUMMY_PASSWORD_HASH = _password_hasher.hash("aurora-dummy-password-verification-only")


def hash_password(password: str) -> str:
    """Hash a plaintext password using Argon2id."""
    if not password:
        raise ValueError("Password cannot be empty.")

    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against an Argon2id hash."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """Check whether a stored password hash needs updated parameters."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except (VerificationError, InvalidHashError):
        return True
