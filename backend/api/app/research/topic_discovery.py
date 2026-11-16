import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.research.trend_ranking import (
    TrendObservation,
    rank_trend_observations,
)

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "the", "to", "what",
    "when", "why", "with", "works",
}


@dataclass(frozen=True, slots=True)
class TopicCandidate:
    """A profile-derived topic candidate and its deterministic ranking signals."""

    topic: str
    relevance: float
    novelty: float
    score: float


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
        if token not in _STOP_WORDS and len(token) > 1
    )


def _similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _is_prohibited(topic: str, prohibited_topics: Sequence[str]) -> bool:
    topic_folded = topic.casefold()
    topic_tokens = _tokens(topic)
    for prohibited in prohibited_topics:
        phrase = prohibited.strip().casefold()
        if not phrase:
            continue
        prohibited_tokens = _tokens(phrase)
        if phrase in topic_folded or (
            prohibited_tokens and prohibited_tokens.issubset(topic_tokens)
        ):
            return True
    return False


def build_topic_candidates(profile: dict) -> tuple[str, ...]:
    """Build bounded, deterministic candidates from an immutable channel profile.

    This is a local-first ideation baseline, not a live trend or news detector.
    Candidate generation deliberately uses only owner-configured profile data.
    """

    niche = str(profile.get("niche", "")).strip()
    if not niche:
        raise ValueError("The channel editorial profile does not contain a usable niche.")

    audience = str(profile.get("target_audience", "")).strip()
    language = str(profile.get("language", "en")).strip()
    boundaries = profile.get("topic_boundaries", [])
    if not isinstance(boundaries, list):
        boundaries = []
    focus_items = [
        item.strip()
        for item in boundaries
        if isinstance(item, str) and item.strip()
    ][:8]
    prohibited = profile.get("prohibited_topics", [])
    prohibited_items = [
        item for item in prohibited if isinstance(item, str) and item.strip()
    ] if isinstance(prohibited, list) else []

    suffix = []
    if audience:
        suffix.append(f"Audience: {audience}")
    if language:
        suffix.append(f"Language: {language}")
    context = ". ".join(suffix)

    candidates: list[str] = []
    if focus_items:
        candidates.append(
            ". ".join([niche, f"Focus: {', '.join(focus_items)}", context]).strip(". ")
        )
        for focus in focus_items:
            candidates.append(
                ". ".join([niche, f"Focus: {focus}", context]).strip(". ")
            )
            candidates.append(
                ". ".join(
                    [niche, f"Explainer: how {focus} works", context]
                ).strip(". ")
            )
    else:
        candidates.append(". ".join([niche, context]).strip(". "))

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate[:300].strip()
        key = candidate.casefold()
        if not candidate or key in seen or _is_prohibited(candidate, prohibited_items):
            continue
        seen.add(key)
        unique.append(candidate)

    if not unique:
        raise ValueError("The editorial profile produced no permitted topic candidates.")
    return tuple(unique)


def rank_topic_candidates(
    profile: dict,
    recent_topics: Sequence[str] = (),
) -> tuple[TopicCandidate, ...]:
    """Rank candidates by profile relevance and distance from recent channel topics.

    Recent topics must be supplied from the same channel and tenant by the caller.
    With no history, preserve the existing profile-query behavior as the first choice.
    """

    candidates = build_topic_candidates(profile)
    boundaries = profile.get("topic_boundaries", [])
    if not isinstance(boundaries, list):
        boundaries = []
    profile_text = " ".join(
        str(profile.get(key, ""))
        for key in ("niche", "target_audience", "language")
    ) + " " + " ".join(
        item for item in boundaries if isinstance(item, str)
    )
    profile_tokens = _tokens(profile_text)

    ranked: list[TopicCandidate] = []
    for index, topic in enumerate(candidates):
        topic_tokens = _tokens(topic)
        relevance = (
            len(topic_tokens & profile_tokens) / len(profile_tokens)
            if profile_tokens
            else 0.0
        )
        novelty = (
            1.0 - max((_similarity(topic, previous) for previous in recent_topics), default=0.0)
        )
        score = 0.75 * novelty + 0.25 * relevance
        if not recent_topics and index == 0:
            score += 1.0
        ranked.append(
            TopicCandidate(
                topic=topic,
                relevance=relevance,
                novelty=novelty,
                score=score,
            )
        )

    return tuple(
        sorted(
            ranked,
            key=lambda candidate: (-candidate.score, -candidate.novelty, candidate.topic),
        )
    )


def select_research_topic(profile: dict, recent_topics: Sequence[str] = ()) -> str:
    """Select the highest-ranked permitted topic for one channel's next research run."""

    ranked = rank_topic_candidates(profile, recent_topics)
    return ranked[0].topic


def is_topic_permitted(topic: str, profile: dict) -> bool:
    """Return whether a discovery lead respects the channel's prohibited topics."""

    prohibited = profile.get("prohibited_topics", [])
    if not isinstance(prohibited, list):
        prohibited = []
    prohibited_items = [
        item for item in prohibited if isinstance(item, str) and item.strip()
    ]
    return not _is_prohibited(topic, prohibited_items)


def select_trend_research_topic(
    profile: dict,
    observations: Sequence[TrendObservation],
    recent_topics: Sequence[str] = (),
    *,
    now: datetime,
) -> str | None:
    """Select a fresh, relevant trend lead without treating it as evidence."""
    permitted = tuple(
        observation
        for observation in observations
        if is_topic_permitted(observation.topic, profile)
    )
    if not permitted:
        return None

    ranked = rank_trend_observations(
        permitted,
        profile=profile,
        recent_topics=recent_topics,
        now=now,
    )
    for candidate in ranked:
        if candidate.relevance > 0:
            return candidate.topic
    return None
