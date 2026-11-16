import pytest

from app.services.youtube_publishing import (
    UNVERIFIED_LICENSE_LINE,
    YouTubePublishingNotReadyError,
    build_youtube_description,
)


def test_manual_rights_override_preserves_attribution_and_discloses_unverified_license():
    credits = [
        {
            "attribution": "Example Research",
            "source_uri": "https://example.com/research",
            "license_ids": [],
            "license_urls": [],
        }
    ]

    with pytest.raises(YouTubePublishingNotReadyError):
        build_youtube_description("A short explanation.", credits)

    description = build_youtube_description(
        "A short explanation.",
        credits,
        allow_unverified_rights=True,
    )

    assert "Sources and credits" in description
    assert "Example Research" in description
    assert "Source: https://example.com/research" in description
    assert UNVERIFIED_LICENSE_LINE in description


def test_manual_rights_override_does_not_allow_unsafe_source_urls():
    credits = [
        {
            "attribution": "Example Research",
            "source_uri": "http://example.com/research",
            "license_ids": [],
            "license_urls": [],
        }
    ]

    with pytest.raises(YouTubePublishingNotReadyError):
        build_youtube_description(
            "A short explanation.",
            credits,
            allow_unverified_rights=True,
        )
