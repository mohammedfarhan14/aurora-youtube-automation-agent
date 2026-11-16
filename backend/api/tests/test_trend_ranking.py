from datetime import UTC, datetime, timedelta

import pytest

from app.research.trend_ranking import (
    TrendObservation,
    normalize_topic_key,
    rank_trend_observations,
)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def profile():
    return {
        "niche": "space science",
        "target_audience": "curious adults",
        "language": "en",
        "topic_boundaries": ["astronomy", "space exploration"],
    }


def observation(topic, *, provider="provider-a", hours_ago=1, momentum=0.5):
    observed_at = NOW - timedelta(hours=hours_ago)
    return TrendObservation(
        provider=provider,
        topic=topic,
        observed_at=observed_at,
        retrieved_at=NOW,
        momentum_score=momentum,
        metric_name="relative_interest",
        metric_value=42,
        metric_unit="index",
    )


def test_trend_observation_requires_timezone_aware_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        TrendObservation(
            provider="test",
            topic="space science",
            observed_at=datetime(2026, 11, 12),
            retrieved_at=NOW,
        )


def test_trend_observation_rejects_invalid_provider_scores():
    with pytest.raises(ValueError, match="between 0 and 1"):
        observation("space science", momentum=1.1)


def test_topic_key_normalizes_case_and_punctuation():
    assert normalize_topic_key("  Artemis-II: Moon Mission! ") == "artemis ii moon mission"


def test_ranking_is_deterministic_and_exposes_score_components(profile):
    observations = [
        observation("Space telescope discoveries", momentum=0.9),
        observation("Space telescope discoveries", provider="provider-b", momentum=0.7),
        observation("Local cooking recipes", momentum=1.0),
    ]

    first = rank_trend_observations(
        observations, profile=profile, recent_topics=[], now=NOW
    )
    second = rank_trend_observations(
        observations, profile=profile, recent_topics=[], now=NOW
    )

    assert first == second
    assert first[0].topic == "Space telescope discoveries"
    candidate = next(item for item in first if item.topic == "Space telescope discoveries")
    assert candidate.providers == ("provider-a", "provider-b")
    assert candidate.observation_count == 2
    assert candidate.provider_diversity == 0.5
    assert 0 <= candidate.score <= 1


def test_stale_and_future_observations_are_excluded(profile):
    observations = [
        observation("Stale space event", hours_ago=100),
        TrendObservation(
            provider="provider-a",
            topic="Future space event",
            observed_at=NOW + timedelta(hours=1),
            retrieved_at=NOW,
        ),
        observation("Fresh astronomy discovery", hours_ago=2),
    ]

    ranked = rank_trend_observations(
        observations, profile=profile, recent_topics=[], now=NOW
    )

    assert [item.topic for item in ranked] == ["Fresh astronomy discovery"]


def test_recent_topics_reduce_novelty(profile):
    topic = "New astronomy discovery"
    ranked = rank_trend_observations(
        [observation(topic)],
        profile=profile,
        recent_topics=[topic],
        now=NOW,
    )

    assert ranked[0].novelty == 0.0


def test_duplicate_topic_observations_aggregate_across_providers(profile):
    ranked = rank_trend_observations(
        [
            observation("New space telescope"),
            observation(" NEW SPACE TELESCOPE! ", provider="provider-b"),
        ],
        profile=profile,
        recent_topics=[],
        now=NOW,
    )

    assert len(ranked) == 1
    assert ranked[0].providers == ("provider-a", "provider-b")
    assert ranked[0].observation_count == 2


def test_ranker_rejects_naive_clock_and_unbounded_limits(profile):
    with pytest.raises(ValueError, match="timezone-aware"):
        rank_trend_observations(
            [], profile=profile, recent_topics=[], now=datetime(2026, 11, 12)
        )
    with pytest.raises(ValueError, match="result limit"):
        rank_trend_observations([], profile=profile, recent_topics=[], now=NOW, limit=101)


def test_provider_raw_metric_is_not_used_as_cross_provider_score(profile):
    ranked = rank_trend_observations(
        [
            TrendObservation(
                provider="provider-a",
                topic="Space news",
                observed_at=NOW - timedelta(hours=1),
                retrieved_at=NOW,
                metric_name="views",
                metric_value=10_000_000,
                metric_unit="views",
            ),
            TrendObservation(
                provider="provider-b",
                topic="Space news",
                observed_at=NOW - timedelta(hours=1),
                retrieved_at=NOW,
                metric_name="search_index",
                metric_value=1,
                metric_unit="index",
            ),
        ],
        profile=profile,
        recent_topics=[],
        now=NOW,
    )

    assert ranked[0].momentum == 0.0
    assert ranked[0].provider_diversity == 0.5


def test_trend_observation_bounds_untrusted_metadata():
    with pytest.raises(ValueError, match="topic must not exceed"):
        observation("x" * 501)
    with pytest.raises(ValueError, match="provider must not exceed"):
        observation("space", provider="p" * 81)
    with pytest.raises(ValueError, match="finite"):
        observation("space", momentum=float("nan"))
