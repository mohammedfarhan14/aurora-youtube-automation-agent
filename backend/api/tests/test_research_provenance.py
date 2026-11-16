from datetime import UTC, datetime

import pytest

from app.research.contracts import ResearchDocument
from app.research.provenance import assess_source_provenance


def document(url, content, *, source_type="webpage"):
    return ResearchDocument(
        canonical_url=url,
        title="Test source",
        publisher="Test publisher",
        source_type=source_type,
        published_at=None,
        retrieved_at=datetime(2026, 11, 12, tzinfo=UTC),
        content=content,
        evidence=(),
    )


def test_classifies_source_type_and_records_normalized_publisher_domain():
    result = assess_source_provenance((
        document("https://www.example.org/report", "unique source text"),
        document("https://journals.example.edu/paper", "scholarly text", source_type="paper"),
        document("https://agency.gov.in/report", "official data"),
        document("https://en.wikipedia.org/wiki/Test", "reference text"),
    ))

    assert result[0].publisher_domain == "example.org"
    assert result[0].source_class == "unknown"
    assert result[1].source_class == "scholarly"
    assert result[2].source_class == "official_domain"
    assert result[3].source_class == "reference"
    assert all(item.classification_version == "1" for item in result)


def test_same_publisher_hostname_is_one_independence_group():
    result = assess_source_provenance((
        document("https://www.example.org/a", "different article one"),
        document("https://example.org/b", "different article two"),
    ))

    assert result[0].independence_group_key == result[1].independence_group_key
    assert result[0].independence_basis == "publisher_domain"


def test_exact_cross_publisher_content_is_one_syndication_group():
    content = "A sufficiently long identical report body that can be syndicated across publishers."
    result = assess_source_provenance((
        document("https://publisher-one.example/article", content),
        document("https://publisher-two.example/story", content),
    ))

    assert result[0].independence_group_key == result[1].independence_group_key
    assert result[0].independence_basis == "exact_content"


def test_near_duplicate_cross_publisher_content_is_grouped():
    original = (
        "The research team published a detailed analysis about renewable energy "
        "storage systems and their performance across several different regions."
    )
    lightly_edited = (
        "The research team published a detailed analysis about renewable energy "
        "storage systems and their performance across several different regions today."
    )
    result = assess_source_provenance((
        document("https://publisher-one.example/article", original),
        document("https://publisher-two.example/story", lightly_edited),
    ))

    assert result[0].independence_group_key == result[1].independence_group_key
    assert result[0].independence_basis == "near_duplicate_content"


def test_unrelated_publishers_remain_separate_groups():
    result = assess_source_provenance((
        document("https://one.example/article", "first unique source body"),
        document("https://two.example/article", "second unique source body"),
    ))

    assert result[0].independence_group_key != result[1].independence_group_key
    assert result[0].independence_basis == "unique_source"


def test_provenance_assessment_rejects_unbounded_content_and_document_counts():
    with pytest.raises(ValueError, match="content exceeds"):
        assess_source_provenance((
            document("https://example.org/large", "x" * 200_001),
        ))
    with pytest.raises(ValueError, match="At most"):
        assess_source_provenance(tuple(
            document(f"https://example-{index}.org/a", f"unique content {index}")
            for index in range(101)
        ))
