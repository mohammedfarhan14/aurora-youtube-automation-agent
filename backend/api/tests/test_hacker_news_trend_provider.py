import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.research.providers import hacker_news


class FakeClient:
    def __init__(self, *, allowed_hosts):
        self.allowed_hosts = allowed_hosts
        self.closed = False
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        if url.endswith("/topstories.json"):
            return json.dumps([11, 12, 13]).encode(), "application/json", datetime.now(UTC)
        story_id = int(url.rsplit("/", 1)[1].removesuffix(".json"))
        payload = {
            11: {"id": 11, "type": "story", "title": "Space telescope finds new planet",
                 "time": 1794484800, "score": 300},
            12: {"id": 12, "type": "story", "title": "New database engine released",
                 "time": 1794481200, "score": 150},
            13: {"id": 13, "type": "comment", "text": "not a trend"},
        }[story_id]
        return json.dumps(payload).encode(), "application/json", datetime.now(UTC)

    async def close(self):
        self.closed = True


def test_hacker_news_provider_maps_top_stories_to_ranked_observations(monkeypatch):
    clients = []

    def client_factory(*, allowed_hosts):
        client = FakeClient(allowed_hosts=allowed_hosts)
        clients.append(client)
        return client

    monkeypatch.setattr(hacker_news, "SafeResearchHttpClient", client_factory)
    result = asyncio.run(hacker_news.HackerNewsTrendProvider().discover(limit=3))

    assert len(result) == 2
    assert result[0].provider == "hacker_news"
    assert result[0].topic == "Space telescope finds new planet"
    assert result[0].momentum_score == 1.0
    assert result[0].metric_value == 300.0
    assert result[0].source_url == "https://news.ycombinator.com/item?id=11"
    assert result[1].momentum_score == 0.0
    assert clients[0].allowed_hosts == frozenset({"hacker-news.firebaseio.com"})
    assert clients[0].closed is True


@pytest.mark.parametrize("limit", [0, 31, -1])
def test_hacker_news_provider_rejects_unbounded_limits(limit):
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(hacker_news.HackerNewsTrendProvider().discover(limit=limit))


def test_hacker_news_decoder_rejects_invalid_payloads():
    with pytest.raises(hacker_news.ResearchFetchError):
        hacker_news._decode_json(b"<html/>", "text/html")
    with pytest.raises(hacker_news.ResearchFetchError):
        hacker_news._decode_json(b"{bad", "application/json")
