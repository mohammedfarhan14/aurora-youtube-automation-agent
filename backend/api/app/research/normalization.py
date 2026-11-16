from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit


def normalize_source_url(value: str) -> str:
    """Normalize an HTTP(S) URL without changing its query semantics."""
    value = value.strip()

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Source URL is invalid.") from exc

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Source URL must be a valid HTTP(S) URL.")

    scheme = parsed.scheme.lower()
    hostname = hostname.lower()

    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    default_port = (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"

    return urlunsplit(
        (
            scheme,
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def content_sha256(value: str) -> str:
    """Return the SHA-256 hex digest of normalized text."""
    normalized = value.strip()
    if not normalized:
        raise ValueError("Content must not be empty.")

    return sha256(normalized.encode("utf-8")).hexdigest()
