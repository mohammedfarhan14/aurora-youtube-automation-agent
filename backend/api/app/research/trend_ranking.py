"""Provider-neutral, deterministic ranking for live trend observations.

Trend observations are discovery signals only. They are never factual evidence.
Adapters must normalize momentum within their own provider/metric before ranking;
raw counts from unrelated providers are not comparable.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite

_MAX_OBSERVATIONS = 1_000
_MAX_RESULTS = 100
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "the", "to", "what",
    "when", "why", "with",
}


@dataclass(frozen=True, slots=True)
class TrendObservation:
    """One provider's time-stamped observation about a candidate topic."""

    provider: str
    topic: str
    observed_at: datetime
    retrieved_at: datetime
    momentum_score: float | None = None
    metric_name: str | None = None
    metric_value: float | None = None
    metric_unit: str | None = None
    geography: str | None = None
    language: str | None = None
    source_url: str | None = None
    publisher: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("Trend observation provider must not be empty.")
        if len(self.provider) > 80:
            raise ValueError("Trend observation provider must not exceed 80 characters.")
        if not isinstance(self.topic, str) or not self.topic.strip():
            raise ValueError("Trend observation topic must not be empty.")
        if len(self.topic) > 500:
            raise ValueError("Trend observation topic must not exceed 500 characters.")
        for name, value, maximum in (
            ("metric_name", self.metric_name, 80),
            ("metric_unit", self.metric_unit, 40),
            ("geography", self.geography, 80),
            ("language", self.language, 32),
            ("source_url", self.source_url, 2048),
            ("publisher", self.publisher, 200),
        ):
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                raise ValueError(f"Trend observation {name} is invalid or too long.")
        for name, value in (
            ("observed_at", self.observed_at),
            ("retrieved_at", self.retrieved_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"Trend observation {name} must be timezone-aware.")
        if self.momentum_score is not None and (
            not isfinite(self.momentum_score) or not 0.0 <= self.momentum_score <= 1.0
        ):
            raise ValueError("Trend momentum score must be finite and between 0 and 1.")
        if self.metric_value is not None and (
            not isfinite(self.metric_value) or self.metric_value < 0
        ):
            raise ValueError("Trend metric value must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class RankedTrendCandidate:
    """An explainable aggregate of observations for one normalized topic."""

    topic: str
    topic_key: str
    providers: tuple[str, ...]
    observation_count: int
    latest_observed_at: datetime
    momentum: float
    freshness: float
    relevance: float
    novelty: float
    provider_diversity: float
    score: float


def normalize_topic_key(value: str) -> str:
    """Normalize casing and punctuation without guessing semantic equivalence."""
    tokens = re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
    return " ".join(tokens)


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token for token in normalize_topic_key(value).split()
        if token not in _STOP_WORDS and len(token) > 1
    )


def _similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _relevance(topic: str, profile: dict) -> float:
    profile_text = " ".join(
        str(profile.get(key, ""))
        for key in ("niche", "target_audience", "language")
    )
    boundaries = profile.get("topic_boundaries", [])
    if isinstance(boundaries, list):
        profile_text += " " + " ".join(
            item for item in boundaries if isinstance(item, str)
        )
    profile_tokens = _tokens(profile_text)
    topic_tokens = _tokens(topic)
    if not profile_tokens or not topic_tokens:
        return 0.0
    return len(profile_tokens & topic_tokens) / len(profile_tokens)


def rank_trend_observations(
    observations: tuple[TrendObservation, ...] | list[TrendObservation],
    *,
    profile: dict,
    recent_topics: tuple[str, ...] | list[str],
    now: datetime,
    max_age: timedelta = timedelta(hours=72),
    limit: int = 20,
) -> tuple[RankedTrendCandidate, ...]:
    """Rank bounded observations using explainable provider-aware features.

    Momentum must be normalized by the adapter within the same provider and
    metric. Freshness, channel relevance, novelty and provider diversity are
    independently exposed so callers can explain every score component.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Ranking time must be timezone-aware.")
    if max_age <= timedelta(0):
        raise ValueError("Maximum trend age must be positive.")
    if not 1 <= limit <= _MAX_RESULTS:
        raise ValueError(f"Trend result limit must be between 1 and {_MAX_RESULTS}.")
    if len(observations) > _MAX_OBSERVATIONS:
        raise ValueError(f"At most {_MAX_OBSERVATIONS} trend observations may be ranked.")

    grouped: dict[str, list[TrendObservation]] = defaultdict(list)
    for observation in observations:
        if not isinstance(observation, TrendObservation):
            raise TypeError("Trend observations must use the TrendObservation contract.")
        key = normalize_topic_key(observation.topic)
        if not key:
            continue
        age = now - observation.observed_at
        if age < -timedelta(minutes=5) or age > max_age:
            continue
        grouped[key].append(observation)

    ranked: list[RankedTrendCandidate] = []
    for key, items in grouped.items():
        latest = max(item.observed_at for item in items)
        age = max(timedelta(0), now - latest)
        freshness = max(0.0, 1.0 - age.total_seconds() / max_age.total_seconds())
        momentum_values = [
            item.momentum_score for item in items if item.momentum_score is not None
        ]
        momentum = max(momentum_values, default=0.0)
        providers = tuple(sorted({item.provider for item in items}))
        provider_diversity = min(1.0, max(0, len(providers) - 1) / 2.0)
        topic = min(
            (item.topic.strip() for item in items),
            key=lambda value: (value.casefold(), value),
        )
        relevance = _relevance(topic, profile)
        novelty = 1.0 - max(
            (_similarity(topic, previous) for previous in recent_topics),
            default=0.0,
        )
        score = (
            0.35 * momentum
            + 0.20 * freshness
            + 0.25 * relevance
            + 0.15 * novelty
            + 0.05 * provider_diversity
        )
        ranked.append(
            RankedTrendCandidate(
                topic=topic,
                topic_key=key,
                providers=providers,
                observation_count=len(items),
                latest_observed_at=latest,
                momentum=momentum,
                freshness=freshness,
                relevance=relevance,
                novelty=novelty,
                provider_diversity=provider_diversity,
                score=score,
            )
        )

    return tuple(
        sorted(
            ranked,
            key=lambda candidate: (
                -candidate.score,
                -candidate.freshness,
                candidate.topic_key,
            ),
        )[:limit]
    )
