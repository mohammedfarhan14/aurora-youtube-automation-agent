import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.llm.contracts import LLMResponse, LLMUsage
from app.story.context import (
    StoryEvidence,
    StoryResearchContext,
    StorySource,
)
from app.story.contracts import StoryScene
from app.story.generation import (
    STORY_PROMPT_VERSION,
    StoryGenerationError,
    _balance_scene_durations,
    _messages,
    generate_story,
)


def make_context(*, include_evidence: bool = True) -> StoryResearchContext:
    source_id = uuid4()
    evidence = (
        (
            StoryEvidence(
                id=uuid4(),
                excerpt="Solar cells convert light into electrical energy.",
                locator="Overview",
            ),
        )
        if include_evidence
        else ()
    )
    source = StorySource(
        id=source_id,
        canonical_url="https://example.org/solar",
        title="Solar cell overview",
        publisher="Example Institute",
        source_type="webpage",
        published_at=None,
        retrieved_at=datetime.now(UTC),
        evidence=evidence,
    )
    return StoryResearchContext(
        research_run_id=uuid4(),
        organization_id=uuid4(),
        topic="How solar cells work",
        sources=(source,),
        included_evidence_count=len(evidence),
        omitted_evidence_count=0,
        omitted_source_count=0,
        context_characters=200,
    )


def test_generation_prompt_guards_live_status_claims_and_scene_text():
    system_message = _messages(make_context())[0].content

    assert "distinguish proposals from enacted decisions" in system_message
    assert "Never request logos, flags, signage, labels, text" in system_message
    assert "never make scene directions quote narration" in system_message
    assert "preferably 4-6 visually distinct scenes" in system_message
    assert "Do not reuse the same framing for adjacent scenes" in system_message


def test_scene_durations_follow_narration_density_instead_of_equal_model_split():
    scenes = (
        StoryScene(
            visual_direction="First shot",
            narration_segment=(
                "One two three four five six seven eight nine ten eleven "
                "twelve thirteen fourteen fifteen."
            ),
            duration_seconds=15,
        ),
        StoryScene(
            visual_direction="Second shot",
            narration_segment=(
                "The longer second narration segment has more words and should receive "
                "more scene time for a natural spoken pace across this short video."
            ),
            duration_seconds=15,
        ),
    )

    balanced = _balance_scene_durations(scenes, 30)

    assert sum(scene.duration_seconds for scene in balanced) == 30
    assert balanced[1].duration_seconds > balanced[0].duration_seconds
    assert balanced[0].duration_seconds != 15


def valid_draft() -> str:
    return json.dumps(
        {
            "title": "How sunlight becomes electricity",
            "hook": "How can sunlight power a home?",
            "beats": [
                {"heading": "Light arrives", "summary": "Sunlight reaches a solar cell."},
                {"heading": "Current flows", "summary": "The cell produces electrical current."},
            ],
            "narration": (
                "Sunlight delivers energy to a solar cell, where particles of light are "
                "absorbed by semiconductor material. That energy can free electrons, "
                "leaving mobile charges inside the cell. Electric fields guide those "
                "charges in a preferred direction. When the cell connects to a circuit, "
                "their movement becomes an electric current. The panel does not store "
                "sunlight; it converts incoming light into electricity that can power "
                "devices or charge a battery."
            ),
            "visual_direction": "Show a simple cross-section of a photovoltaic cell.",
            "scenes": [
                {
                    "visual_direction": "Sunlight reaches the panel.",
                    "narration_segment": (
                        "Sunlight delivers energy to a solar cell, where particles of "
                        "light are absorbed by semiconductor material."
                    ),
                    "duration_seconds": 7,
                },
                {
                    "visual_direction": "Show freed electrons moving within the cell.",
                    "narration_segment": (
                        "That energy can free electrons, leaving mobile charges inside the cell."
                    ),
                    "duration_seconds": 7,
                },
                {
                    "visual_direction": "Show an electric field guiding charges toward a wire.",
                    "narration_segment": (
                        "Electric fields guide those charges in a preferred direction. "
                        "When the cell connects to a circuit,"
                    ),
                    "duration_seconds": 8,
                },
                {
                    "visual_direction": "Show current powering a small household device.",
                    "narration_segment": (
                        "their movement becomes an electric current. The panel does not "
                        "store sunlight; it converts incoming light into electricity that "
                        "can power devices or charge a battery."
                    ),
                    "duration_seconds": 8,
                },
            ],
        }
    )


class RecordingProvider:
    def __init__(self, content: str):
        self.content = content
        self.request = None
        self.requests = []

    async def generate(self, request):
        self.request = request
        self.requests.append(request)
        return LLMResponse(
            content=self.content,
            provider="mock",
            model=request.model,
            finish_reason="stop",
            usage=LLMUsage(input_tokens=100, output_tokens=80, total_tokens=180),
            request_id="test-request",
        )


class SequenceProvider(RecordingProvider):
    def __init__(self, *contents: str):
        super().__init__(contents[0])
        self.contents = list(contents)

    async def generate(self, request):
        self.request = request
        self.requests.append(request)
        content = self.contents.pop(0)
        return LLMResponse(
            content=content,
            provider="mock",
            model=request.model,
            finish_reason="stop",
            usage=LLMUsage(input_tokens=100, output_tokens=80, total_tokens=180),
            request_id=f"test-request-{len(self.requests)}",
        )


def too_long_draft() -> str:
    payload = json.loads(valid_draft())
    payload["narration"] = " ".join(["word"] * 71)
    return json.dumps(payload)


def test_generation_builds_story_and_attaches_provenance_in_application_code():
    context = make_context()
    provider = RecordingProvider(valid_draft())

    result = asyncio.run(generate_story(provider, context, model="qwen2.5:7b"))

    assert result.artifact.title == "How sunlight becomes electricity"
    assert result.artifact.target_duration_seconds == 30
    assert result.artifact.word_budget == 70
    assert result.artifact.factual_claims == ()
    assert result.artifact.source_references[0].source_id == context.sources[0].id
    assert result.artifact.source_references[0].canonical_url == context.sources[0].canonical_url
    assert result.artifact.source_references[0].title == context.sources[0].title
    assert result.artifact.source_references[0].publisher == context.sources[0].publisher
    assert result.artifact.source_references[0].evidence_ids == (context.sources[0].evidence[0].id,)
    assert (
        result.artifact.source_references[0].rights_metadata == context.sources[0].rights_metadata
    )
    assert result.prompt_version == STORY_PROMPT_VERSION
    assert result.provider == "mock"
    assert result.model == "qwen2.5:7b"
    assert result.request_id == "test-request"
    assert result.usage.total_tokens == 180
    assert provider.request.temperature == 0.2
    assert provider.request.max_output_tokens == 2_048
    assert provider.request.response_schema is not None
    schema = provider.request.response_schema
    assert set(schema["required"]) == {
        "title",
        "hook",
        "beats",
        "narration",
        "visual_direction",
        "scenes",
    }
    assert "$defs" not in schema


def test_generation_prompt_marks_retrieved_source_text_as_untrusted_data():
    context = make_context()
    provider = RecordingProvider(valid_draft())

    asyncio.run(generate_story(provider, context, model="local-model"))

    prompt = provider.request.messages[0].content
    payload = provider.request.messages[1].content
    assert "untrusted" in prompt.lower()
    assert "original wording" in prompt.lower()
    assert "Do not invent" in prompt
    assert "CONTEXT_JSON" in payload
    assert context.sources[0].evidence[0].excerpt[:900] in payload
    assert str(context.sources[0].id) not in payload
    assert "rights_metadata" not in payload


def test_qwen3_story_generation_disables_thinking_and_uses_local_output_budget():
    provider = RecordingProvider(valid_draft())
    asyncio.run(generate_story(provider, make_context(), model="qwen3:4b"))

    assert "/no_think" in provider.request.messages[-1].content
    assert provider.request.max_output_tokens == 2_048
    assert provider.request.response_format == "json_object"
    assert provider.request.response_schema is None


def test_qwen3_story_generation_settings_apply_to_request():
    provider = RecordingProvider(valid_draft())
    asyncio.run(
        generate_story(
            provider,
            make_context(),
            model="qwen3:4b",
            qwen_thinking_mode="on",
            max_output_tokens=2048,
            timeout_seconds=300,
        )
    )

    assert "/think" in provider.request.messages[-1].content
    assert "/no_think" not in provider.request.messages[-1].content
    assert provider.request.max_output_tokens == 2048
    assert provider.request.timeout_seconds == 300


@pytest.mark.parametrize(
    "bad_output",
    [
        "not json",
        json.dumps({"title": "Only a title"}),
        valid_draft().replace('"duration_seconds": 7', '"duration_seconds": 11'),
        too_long_draft(),
    ],
)
def test_generation_rejects_malformed_or_invalid_story_output(bad_output):
    provider = RecordingProvider(bad_output)

    with pytest.raises(StoryGenerationError):
        asyncio.run(generate_story(provider, make_context(), model="local-model"))


def test_generation_retries_short_narration_and_records_both_usages():
    short = json.loads(valid_draft())
    short["narration"] = "Solar cells turn light into electricity."
    provider = SequenceProvider(json.dumps(short), valid_draft())

    result = asyncio.run(generate_story(provider, make_context(), model="local-model"))

    assert len(provider.requests) == 2
    assert "58–70 words" in provider.requests[1].messages[-1].content
    assert "Solar cells turn light into electricity." in provider.requests[1].messages[-2].content
    assert result.artifact.word_budget == 70
    assert len(result.artifact.narration.split()) == 69
    assert result.usage.total_tokens == 360


def test_generation_fails_clearly_when_corrected_narration_is_still_short():
    short = json.loads(valid_draft())
    short["narration"] = "Solar cells turn light into electricity."
    provider = SequenceProvider(json.dumps(short), json.dumps(short), json.dumps(short))

    with pytest.raises(StoryGenerationError, match="does not meet the 58–70 word budget"):
        asyncio.run(generate_story(provider, make_context(), model="local-model"))
    assert len(provider.requests) == 3


def test_generation_allows_two_bounded_length_corrections():
    short = json.loads(valid_draft())
    short["narration"] = "Solar cells turn light into electricity."
    provider = SequenceProvider(json.dumps(short), json.dumps(short), valid_draft())

    result = asyncio.run(generate_story(provider, make_context(), model="local-model"))

    assert len(provider.requests) == 3
    assert "within that range" in provider.requests[1].messages[-1].content
    assert "within that range" in provider.requests[2].messages[-1].content
    assert result.usage.total_tokens == 540
    assert len(result.artifact.narration.split()) == 69


def test_generation_corrects_narration_that_exceeds_the_word_budget():
    provider = SequenceProvider(too_long_draft(), valid_draft())

    result = asyncio.run(generate_story(provider, make_context(), model="local-model"))

    assert len(provider.requests) == 2
    assert "Remove repetition" in provider.requests[1].messages[-1].content
    assert len(result.artifact.narration.split()) == 69


def test_generation_trims_only_a_small_overrun_at_a_complete_sentence_boundary():
    payload = json.loads(valid_draft())
    payload["narration"] += " Solar panels work."
    provider = RecordingProvider(json.dumps(payload))

    result = asyncio.run(generate_story(provider, make_context(), model="local-model"))

    assert len(result.artifact.narration.split()) == 69
    assert result.artifact.narration.endswith("charge a battery.")
    assert (
        " ".join(scene.narration_segment for scene in result.artifact.scenes)
        == result.artifact.narration
    )


def test_generation_rewrites_source_copy_before_accepting_a_story():
    copied_text = " ".join(f"sourceword{index}." for index in range(69))
    context = make_context()
    copied_source = replace(
        context.sources[0],
        evidence=(StoryEvidence(id=uuid4(), excerpt=copied_text, locator="Article body"),),
    )
    context = replace(context, sources=(copied_source,))
    copied_draft = json.loads(valid_draft())
    copied_draft["narration"] = copied_text
    provider = SequenceProvider(json.dumps(copied_draft), valid_draft())

    result = asyncio.run(generate_story(provider, context, model="local-model"))

    assert len(provider.requests) == 2
    assert "No sequence of five" in provider.requests[1].messages[-1].content
    assert result.artifact.narration != copied_text


def test_generation_rejects_source_copy_after_bounded_rewrites():
    copied_text = " ".join(f"sourceword{index}." for index in range(69))
    context = make_context()
    copied_source = replace(
        context.sources[0],
        evidence=(StoryEvidence(id=uuid4(), excerpt=copied_text, locator="Article body"),),
    )
    context = replace(context, sources=(copied_source,))
    copied_draft = json.loads(valid_draft())
    copied_draft["narration"] = copied_text
    response = json.dumps(copied_draft)
    provider = SequenceProvider(response, response, response, response)

    with pytest.raises(StoryGenerationError, match="closely repeats retrieved source text"):
        asyncio.run(generate_story(provider, context, model="local-model"))
    assert len(provider.requests) == 4


def test_generation_performs_final_targeted_originality_rewrite():
    copied_text = " ".join(f"sourceword{index}." for index in range(69))
    context = make_context()
    copied_source = replace(
        context.sources[0],
        evidence=(StoryEvidence(id=uuid4(), excerpt=copied_text, locator="Article body"),),
    )
    context = replace(context, sources=(copied_source,))
    copied_draft = json.loads(valid_draft())
    copied_draft["narration"] = copied_text
    copied_response = json.dumps(copied_draft)
    provider = SequenceProvider(
        copied_response,
        copied_response,
        copied_response,
        valid_draft(),
    )

    result = asyncio.run(generate_story(provider, context, model="local-model"))

    assert len(provider.requests) == 4
    assert "Final originality correction" in provider.requests[-1].messages[-1].content
    final_prompt = provider.requests[-1].messages[-1].content
    assert "sourceword0 sourceword1 sourceword2 sourceword3 sourceword4" in final_prompt
    assert result.artifact.narration != copied_text


def test_generation_rejects_model_supplied_provenance_fields():
    payload = json.loads(valid_draft())
    payload["source_references"] = [{"source_id": str(uuid4()), "evidence_ids": []}]
    provider = RecordingProvider(json.dumps(payload))

    with pytest.raises(StoryGenerationError, match="did not satisfy"):
        asyncio.run(generate_story(provider, make_context(), model="local-model"))


def test_generation_rejects_empty_research_context_before_provider_call():
    context = StoryResearchContext(
        research_run_id=uuid4(),
        organization_id=uuid4(),
        topic="Empty research",
        sources=(),
        included_evidence_count=0,
        omitted_evidence_count=0,
        omitted_source_count=0,
        context_characters=14,
    )
    provider = RecordingProvider(valid_draft())

    with pytest.raises(StoryGenerationError, match="no sources"):
        asyncio.run(generate_story(provider, context, model="local-model"))

    assert provider.request is None


@pytest.mark.parametrize("model", ["", " "])
def test_generation_rejects_missing_model_identifier(model):
    provider = RecordingProvider(valid_draft())

    with pytest.raises(ValueError, match="model"):
        asyncio.run(generate_story(provider, make_context(), model=model))


def test_generation_preserves_source_without_usable_evidence():
    context = make_context(include_evidence=False)
    provider = RecordingProvider(valid_draft())

    result = asyncio.run(generate_story(provider, context, model="local-model"))

    assert len(result.artifact.source_references) == 1
    assert result.artifact.source_references[0].source_id == context.sources[0].id
    assert result.artifact.source_references[0].evidence_ids == ()


@pytest.mark.anyio
async def test_generate_story_applies_channel_editorial_profile_as_bounded_context():
    context = make_context()
    context = StoryResearchContext(
        research_run_id=context.research_run_id,
        organization_id=context.organization_id,
        topic=context.topic,
        sources=context.sources,
        included_evidence_count=context.included_evidence_count,
        omitted_evidence_count=context.omitted_evidence_count,
        omitted_source_count=context.omitted_source_count,
        context_characters=context.context_characters,
        editorial_profile={
            "niche": "space science",
            "target_audience": "curious adults",
            "language": "en",
            "tone": "documentary",
            "format": "educational",
            "topic_boundaries": ["astronomy"],
            "prohibited_topics": ["unsupported health claims"],
            "additional_constraints": ["Explain technical terms"],
        },
        approved_analytics_learnings=("Review the opening and pacing of recent videos.",),
    )
    provider = RecordingProvider(valid_draft())

    result = await generate_story(provider, context, model="test-model")

    assert result.prompt_version == STORY_PROMPT_VERSION
    assert "58-70 narration words" in provider.request.messages[0].content
    user_message = provider.request.messages[1].content
    assert "CONTEXT_JSON" in user_message
    assert '"niche":"space science"' in user_message
    assert '"prohibited_topics":["unsupported health claims"]' in user_message
    assert (
        '"approved_analytics_learnings":["Review the opening and pacing of recent videos."]'
        in user_message
    )


def test_generation_uses_tenant_duration_preferences_in_prompt_and_artifact():
    payload = json.loads(valid_draft())
    payload["narration"] = (
        "Sunlight delivers energy to a solar cell, where particles of light are absorbed by "
        "semiconductor material. That energy can free electrons, leaving mobile charges inside "
        "the cell. Electric fields guide those charges in a preferred direction. When the cell "
        "connects to a circuit, their movement becomes an electric current. The panel does not "
        "store sunlight; it converts incoming light into electricity that can power devices or "
        "charge a battery. This conversion happens whenever light reaches the active material. "
        "A circuit gives the moving charges a path. In a complete system, an inverter can "
        "also change the electricity into a form used by many household appliances."
    )
    payload["scenes"][0]["duration_seconds"] = 11
    payload["scenes"][1]["duration_seconds"] = 11
    payload["scenes"][2]["duration_seconds"] = 11
    payload["scenes"][3]["duration_seconds"] = 12
    provider = RecordingProvider(json.dumps(payload))

    assert len(payload["narration"].split()) >= 101
    result = asyncio.run(
        generate_story(
            provider,
            make_context(),
            model="local-model",
            target_duration_seconds=45,
        )
    )

    assert result.artifact.target_duration_seconds == 45
    assert result.artifact.word_budget == 105
    prompt = provider.request.messages[0].content
    assert "45-second story" in prompt
    assert "sum to exactly 45 seconds" in prompt


def test_generation_repairs_scene_segments_to_exact_narration_partition():
    payload = json.loads(valid_draft())
    payload["scenes"][0]["narration_segment"] = "A paraphrase that drifts from narration."
    provider = RecordingProvider(json.dumps(payload))

    result = asyncio.run(generate_story(provider, make_context(), model="local-model"))

    assert (
        " ".join(scene.narration_segment for scene in result.artifact.scenes)
        == result.artifact.narration
    )
    assert result.artifact.scenes[0].narration_segment.endswith((".", "!", "?"))


def test_generation_rejects_unsupported_tenant_duration():
    with pytest.raises(ValueError, match="15, 30, or 45"):
        asyncio.run(
            generate_story(
                RecordingProvider(valid_draft()),
                make_context(),
                model="local-model",
                target_duration_seconds=60,
            )
        )
