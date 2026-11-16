import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.workers import story_stage_handlers


def test_generation_and_verification_share_the_same_source_context_limits(monkeypatch):
    expected = object()
    captured = {}

    async def load_context(session, **kwargs):
        captured.update(kwargs)
        return expected

    run = SimpleNamespace(
        research_run_id=uuid4(),
        channel_profile_snapshot={"tone": "documentary"},
        youtube_channel_id=uuid4(),
    )
    organization_id = uuid4()
    monkeypatch.setattr(story_stage_handlers, "load_story_research_context", load_context)

    result = asyncio.run(story_stage_handlers._load_production_story_context(
        object(), organization_id=organization_id, run=run
    ))

    assert result is expected
    assert captured == {
        "organization_id": organization_id,
        "research_run_id": run.research_run_id,
        "editorial_profile": run.channel_profile_snapshot,
        "youtube_channel_id": run.youtube_channel_id,
        "max_sources": 4,
        "max_evidence_per_source": 1,
        "max_context_characters": 10_000,
    }
