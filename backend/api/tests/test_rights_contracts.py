import pytest
from pydantic import ValidationError

from app.rights.contracts import (
    ReuseRestriction,
    RightsMetadata,
    RightsScope,
    RightsStatus,
)


def test_rights_metadata_defaults_to_explicit_unknown_state():
    metadata = RightsMetadata()

    assert metadata.status == RightsStatus.UNKNOWN
    assert metadata.scope == RightsScope.UNKNOWN
    assert metadata.license_ids == ()
    assert metadata.license_urls == ()


def test_cleared_rights_require_a_license_identifier_and_url():
    with pytest.raises(ValidationError, match="Cleared rights require"):
        RightsMetadata(status=RightsStatus.CLEARED)

    metadata = RightsMetadata(
        status=RightsStatus.CLEARED,
        scope=RightsScope.MEDIA_ASSET,
        license_ids=("CC0-1.0",),
        license_urls=("https://creativecommons.org/publicdomain/zero/1.0/",),
    )
    assert metadata.status == RightsStatus.CLEARED


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/license",
        "https://user:password@example.com/license",
        "https://example.com/license#fragment",
        "javascript:alert(1)",
    ],
)
def test_rights_metadata_rejects_unsafe_license_urls(url):
    with pytest.raises(ValidationError):
        RightsMetadata(
            license_ids=("Example-License",),
            license_urls=(url,),
        )


def test_rights_metadata_rejects_duplicate_license_ids():
    with pytest.raises(ValidationError, match="unique"):
        RightsMetadata(
            license_ids=("CC-BY-SA-4.0", "CC-BY-SA-4.0"),
            license_urls=(
                "https://creativecommons.org/licenses/by-sa/4.0/",
                "https://creativecommons.org/licenses/by-sa/4.0/",
            ),
        )


def test_rights_metadata_round_trips_through_json_storage():
    original = RightsMetadata(
        status=RightsStatus.REVIEW_REQUIRED,
        scope=RightsScope.SOURCE_TEXT,
        license_ids=("CC-BY-SA-4.0",),
        license_urls=("https://creativecommons.org/licenses/by-sa/4.0/",),
        rights_holder="Wikipedia contributors",
        creators=("Wikipedia contributors",),
        source_revision_url="https://en.wikipedia.org/w/index.php?oldid=123",
        attribution_text="Wikipedia contributors, revision 123",
        reuse_restrictions=(
            ReuseRestriction.ATTRIBUTION,
            ReuseRestriction.SHARE_ALIKE,
        ),
    )

    restored = RightsMetadata.model_validate(original.model_dump(mode="json"))

    assert restored == original
