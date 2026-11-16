from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.skills.execution import ToolAuthorizationError
from app.story.contracts import StoryArtifact, StoryBeat, StoryScene
from app.workers.story_skill_runtime import execute_audited_story_operation


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def __init__(self, records, *, authorized=True):
        self.records = records
        self.authorized = authorized

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def scalar(self, statement):
        return uuid4() if self.authorized else None

    def begin(self):
        return FakeTransaction()

    def add(self, record):
        self.records.append(record)


def make_artifact():
    return StoryArtifact(
        title="A short science story",
        hook="What makes sunlight useful?",
        beats=(
            StoryBeat(heading="Capture", summary="Solar cells capture light."),
            StoryBeat(heading="Convert", summary="Cells convert light to electricity."),
        ),
        narration="Solar cells capture sunlight and convert it into electrical energy.",
        visual_direction="Show sunlight reaching a solar panel.",
        scenes=(
            StoryScene(
                visual_direction="Sunlight reaches a panel.",
                narration_segment="Solar cells capture sunlight",
                duration_seconds=15,
            ),
            StoryScene(
                visual_direction="The panel powers a light.",
                narration_segment="and convert it into electrical energy.",
                duration_seconds=15,
            ),
        ),
        target_duration_seconds=30,
        word_budget=70,
    )


@pytest.mark.anyio
async def test_story_operation_runs_through_allowlisted_runtime_and_persists_audit():
    records = []
    artifact = make_artifact()
    called = False

    async def operation():
        nonlocal called
        called = True
        return SimpleNamespace(artifact=artifact, provider="mock")

    def session_factory():
        return FakeSession(records)

    result = await execute_audited_story_operation(
        skill_id="story.generation",
        tool_id="story.generate",
        organization_id=uuid4(),
        actor_id=uuid4(),
        request_id="production-stage:123",
        idempotency_key="production-stage:123",
        operation=operation,
        session_factory=session_factory,
    )

    assert called is True
    assert result.artifact is artifact
    assert len(records) == 1
    assert records[0].skill_id == "story.generation"
    assert records[0].tool_id == "story.generate"
    assert records[0].outcome == "success"
    assert records[0].output_sha256 is not None


@pytest.mark.anyio
async def test_story_operation_is_not_invoked_when_actor_membership_is_inactive():
    records = []
    called = False

    async def operation():
        nonlocal called
        called = True
        return SimpleNamespace(artifact=make_artifact())

    def session_factory():
        return FakeSession(records, authorized=False)

    with pytest.raises(ToolAuthorizationError, match="Server-side tool authorization"):
        await execute_audited_story_operation(
            skill_id="story.generation",
            tool_id="story.generate",
            organization_id=uuid4(),
            actor_id=uuid4(),
            request_id="production-stage:denied",
            idempotency_key="production-stage:denied",
            operation=operation,
            session_factory=session_factory,
        )

    assert called is False
    assert len(records) == 1
    assert records[0].outcome == "failure"
    assert records[0].failure_code == "authorization_denied"
