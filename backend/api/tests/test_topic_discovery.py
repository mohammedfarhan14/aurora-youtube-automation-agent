from datetime import UTC, datetime, timedelta

import pytest

from app.research.topic_discovery import (
    build_topic_candidates,
    rank_topic_candidates,
    select_research_topic,
    select_trend_research_topic,
)
from app.research.trend_ranking import TrendObservation


@pytest.fixture
def editorial_profile():
    return {
        "niche": "space science",
        "topic_boundaries": ["astronomy", "space exploration"],
        "target_audience": "curious adults",
        "language": "en",
        "prohibited_topics": ["unsupported medical claims"],
    }


def test_candidate_generation_is_deterministic_and_preserves_profile_context(editorial_profile):
    first = build_topic_candidates(editorial_profile)
    second = build_topic_candidates(editorial_profile)

    assert first == second
    assert first[0] == (
        "space science. Focus: astronomy, space exploration. "
        "Audience: curious adults. Language: en"
    )
    assert all(len(topic) <= 300 for topic in first)


def test_first_selection_preserves_existing_profile_query(editorial_profile):
    selected = select_research_topic(editorial_profile)

    assert selected == build_topic_candidates(editorial_profile)[0]


def test_recent_channel_topic_is_downranked(editorial_profile):
    previous = select_research_topic(editorial_profile)
    ranked = rank_topic_candidates(editorial_profile, [previous])

    assert ranked[0].topic != previous
    assert ranked[0].novelty > 0
    assert ranked[0].score >= ranked[-1].score


def test_prohibited_profile_topics_are_excluded():
    profile = {
        "niche": "space science",
        "topic_boundaries": ["astronomy", "unsupported medical claims"],
        "prohibited_topics": ["unsupported medical claims"],
    }

    candidates = build_topic_candidates(profile)

    assert candidates
    assert all("unsupported medical claims" not in topic.casefold() for topic in candidates)


def test_profile_without_usable_niche_is_rejected():
    with pytest.raises(ValueError, match="usable niche"):
        build_topic_candidates({"niche": "  "})


def test_profile_with_only_prohibited_candidates_is_rejected():
    with pytest.raises(ValueError, match="no permitted topic candidates"):
        build_topic_candidates(
            {
                "niche": "medical claims",
                "topic_boundaries": [],
                "prohibited_topics": ["medical claims"],
            }
        )


def test_channel_trend_selection_prefers_fresh_relevant_permitted_observation(editorial_profile):
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    observations = (
        TrendObservation(
            provider="hacker_news",
            topic="New astronomy telescope discovery",
            observed_at=now - timedelta(hours=1),
            retrieved_at=now,
            momentum_score=0.95,
        ),
        TrendObservation(
            provider="hacker_news",
            topic="Unrelated cooking recipe discussion",
            observed_at=now - timedelta(minutes=5),
            retrieved_at=now,
            momentum_score=1.0,
        ),
    )

    selected = select_trend_research_topic(
        editorial_profile,
        observations,
        now=now,
    )

    assert selected == "New astronomy telescope discovery"


def test_channel_trend_selection_excludes_prohibited_topics(editorial_profile):
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    observations = (
        TrendObservation(
            provider="hacker_news",
            topic="Unsupported medical claims in space science",
            observed_at=now - timedelta(minutes=5),
            retrieved_at=now,
            momentum_score=1.0,
        ),
    )

    assert select_trend_research_topic(
        editorial_profile,
        observations,
        now=now,
    ) is None


def test_channel_trend_selection_returns_none_without_channel_relevance(editorial_profile):
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    observations = (
        TrendObservation(
            provider="hacker_news",
            topic="Cooking recipes and kitchen appliances",
            observed_at=now - timedelta(minutes=5),
            retrieved_at=now,
            momentum_score=1.0,
        ),
    )

    assert select_trend_research_topic(
        editorial_profile,
        observations,
        now=now,
    ) is None
