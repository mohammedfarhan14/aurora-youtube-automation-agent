import hashlib
import secrets


def generate_secure_token() -> str:
    """Generate a cryptographically secure URL-safe token."""

    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Return the SHA-256 hexadecimal digest of a token."""

    if not token:
        raise ValueError("Token cannot be empty.")

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Backward-compatible names for invitation workflows.
generate_invitation_token = generate_secure_token
hash_invitation_token = hash_token
