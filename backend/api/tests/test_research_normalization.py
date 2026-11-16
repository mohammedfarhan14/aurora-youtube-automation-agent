import pytest

from app.research.normalization import (
    content_sha256,
    normalize_source_url,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            " HTTPS://Example.COM:443/article#section ",
            "https://example.com/article",
        ),
        (
            "http://Example.COM:80",
            "http://example.com/",
        ),
        (
            "https://example.com/article?a=1&b=2#evidence",
            "https://example.com/article?a=1&b=2",
        ),
    ],
)
def test_normalize_source_url(source, expected):
    assert normalize_source_url(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "",
        "ftp://example.com/article",
        "https://",
        "https://user:password@example.com/article",
        "not-a-url",
    ],
)
def test_normalize_source_url_rejects_invalid_urls(source):
    with pytest.raises(ValueError):
        normalize_source_url(source)


def test_content_sha256_is_deterministic_and_ignores_outer_whitespace():
    first = content_sha256("  Evidence excerpt  ")
    second = content_sha256("Evidence excerpt")

    assert first == second
    assert len(first) == 64


def test_content_sha256_rejects_blank_content():
    with pytest.raises(ValueError, match="must not be empty"):
        content_sha256("   ")
