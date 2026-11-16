"""Opt-in trend discovery through the official Hacker News Firebase API.

Hacker News scores are retained as provider-specific raw metrics. The ranking
signal is a bounded within-response rank percentile, never a cross-provider
comparison of raw vote counts. Story titles are discovery leads, not evidence.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from app.research.providers.http import SafeResearchHttpClient
from app.research.safe_http import ResearchFetchError
from app.research.trend_ranking import TrendObservation

_API_ROOT = "https://hacker-news.firebaseio.com/v0"
_ALLOWED_HOSTS = frozenset({"hacker-news.firebaseio.com"})
_MAX_STORIES = 30


class HackerNewsTrendProvider:
    """Fetch a bounded top-stories snapshot without activating it by default."""

    async def discover(self, *, limit: int = 20) -> tuple[TrendObservation, ...]:
        if not 1 <= limit <= _MAX_STORIES:
            raise ValueError(f"Hacker News trend limit must be between 1 and {_MAX_STORIES}.")

        observations: list[TrendObservation] = []
        async with asyncio.timeout(45):
            client = SafeResearchHttpClient(allowed_hosts=_ALLOWED_HOSTS)
            try:
                body, content_type, _ = await client.get(f"{_API_ROOT}/topstories.json")
                story_ids = _decode_json(body, content_type)
                if not isinstance(story_ids, list):
                    raise ResearchFetchError("Hacker News returned an invalid story list.")

                bounded_ids = [
                    story_id
                    for story_id in story_ids[:limit]
                    if (
                        isinstance(story_id, int)
                        and not isinstance(story_id, bool)
                        and story_id > 0
                    )
                ]
                stories: list[dict[str, Any]] = []
                for story_id in bounded_ids:
                    item_body, item_type, item_retrieved_at = await client.get(
                        f"{_API_ROOT}/item/{story_id}.json"
                    )
                    item = _decode_json(item_body, item_type)
                    if not isinstance(item, dict):
                        continue
                    if (
                        item.get("type") != "story"
                        or item.get("deleted")
                        or item.get("dead")
                    ):
                        continue
                    title = item.get("title")
                    created = item.get("time")
                    score = item.get("score", 0)
                    if (
                        not isinstance(title, str)
                        or not title.strip()
                        or len(title) > 500
                        or not isinstance(created, int)
                        or isinstance(created, bool)
                        or created <= 0
                        or not isinstance(score, int)
                        or isinstance(score, bool)
                        or score < 0
                    ):
                        continue
                    stories.append({
                        "id": story_id,
                        "title": title.strip(),
                        "created": created,
                        "score": score,
                        "retrieved": item_retrieved_at,
                    })

                # Normalize only within this provider snapshot. Ties receive equal
                # momentum; raw Hacker News points remain descriptive metadata.
                ordered_scores = sorted(
                    {story["score"] for story in stories}, reverse=True
                )
                score_percentiles = {
                    score: (
                        1.0 if len(ordered_scores) == 1
                        else 1.0 - index / (len(ordered_scores) - 1)
                    )
                    for index, score in enumerate(ordered_scores)
                }
                for story in stories:
                    observations.append(
                        TrendObservation(
                            provider="hacker_news",
                            topic=story["title"],
                            observed_at=datetime.fromtimestamp(story["created"], UTC),
                            retrieved_at=story["retrieved"],
                            momentum_score=score_percentiles[story["score"]],
                            metric_name="hacker_news_points",
                            metric_value=float(story["score"]),
                            metric_unit="points",
                            geography="global",
                            source_url=(
                                f"https://news.ycombinator.com/item?id={story['id']}"
                            ),
                            publisher="Hacker News",
                        )
                    )
            finally:
                await client.close()

        return tuple(observations)


def _decode_json(body: bytes, content_type: str) -> Any:
    if content_type != "application/json":
        raise ResearchFetchError("Hacker News API returned an unsupported content type.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchFetchError("Hacker News API returned invalid JSON.") from exc
