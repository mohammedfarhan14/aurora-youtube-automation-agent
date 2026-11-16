import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.llm.contracts import LLMResponse, LLMUsage
from app.llm.providers.openai_compatible import LLMProviderError
from app.rights.contracts import RightsMetadata, RightsScope, RightsStatus
from app.story.context import (
    StoryEvidence,
    StoryResearchContext,
    StorySource,
)
from app.story.contracts import (
    ClaimVerificationStatus,
    SourceReference,
    StoryArtifact,
    StoryBeat,
    StoryScene,
)
from app.story.verification import StoryVerificationError, verify_story_claims


class QueueProvider:
    def __init__(self, *responses: str, name: str = "mock"):
        self.responses = list(responses)
        self.name = name
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        content = self.responses.pop(0)
        return LLMResponse(
            content=content,
            provider=self.name,
            model=request.model,
            finish_reason="stop",
            usage=LLMUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            request_id=f"{self.name}-{len(self.requests)}",
        )


def make_context(*, with_evidence: bool = True, with_source: bool = True):
    if not with_source:
        return StoryResearchContext(
            research_run_id=uuid4(),
            organization_id=uuid4(),
            topic="Unreferenced claim",
            sources=(),
            included_evidence_count=0,
            omitted_evidence_count=0,
            omitted_source_count=0,
            context_characters=18,
        )

    source_id = uuid4()
    evidence = (
        (
            StoryEvidence(
                id=uuid4(),
                excerpt="Solar cells convert sunlight into electrical energy.",
                locator="How it works",
            ),
        )
        if with_evidence
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
        publisher_domain="example.org",
        source_class="unknown",
        independence_group_key="group-example",
        independence_basis="unique_source",
        classification_version="1",
    )
    return StoryResearchContext(
        research_run_id=uuid4(),
        organization_id=uuid4(),
        topic="How solar cells work",
        sources=(source,),
        included_evidence_count=len(evidence),
        omitted_evidence_count=0,
        omitted_source_count=0,
        context_characters=100,
    )


def make_artifact(context: StoryResearchContext) -> StoryArtifact:
    return StoryArtifact(
        title="Solar cells",
        hook="How does sunlight become electricity?",
        beats=(
            StoryBeat(heading="Light", summary="Sunlight reaches a solar cell."),
            StoryBeat(heading="Current", summary="The cell produces electricity."),
        ),
        narration="Solar cells convert sunlight into electrical energy.",
        visual_direction="Show a solar panel and a simple circuit.",
        scenes=(
            StoryScene(
                visual_direction="Sunlight reaches a solar cell.",
                narration_segment="Solar cells convert sunlight",
                duration_seconds=15,
            ),
            StoryScene(
                visual_direction="Show current flowing through a circuit.",
                narration_segment="into electrical energy.",
                duration_seconds=15,
            ),
        ),
        source_references=tuple(
            SourceReference(
                source_id=source.id,
                evidence_ids=tuple(item.id for item in source.evidence),
                canonical_url=source.canonical_url,
                title=source.title,
                publisher=source.publisher,
            )
            for source in context.sources
        ),
    )


def extraction_response(*claims: str) -> str:
    return json.dumps({"claims": [{"claim": claim} for claim in claims]})


def verification_response(*assessments) -> str:
    return json.dumps({"assessments": list(assessments)})


def assessment(index: int, *evidence) -> dict:
    return {
        "claim_index": index,
        "evidence": [
            {
                "source_id": str(source_id),
                "evidence_id": str(evidence_id),
                "relation": relation,
            }
            for source_id, evidence_id, relation in evidence
        ],
    }


def run_verification(context, extraction, verification):
    extraction_provider = QueueProvider(extraction, name="extractor")
    verification_provider = QueueProvider(verification, name="verifier")
    result = asyncio.run(
        verify_story_claims(
            extraction_provider,
            verification_provider,
            make_artifact(context),
            context,
            extraction_model="local-extract",
            verification_model="local-verify",
        )
    )
    return result, extraction_provider, verification_provider


EVALUATION_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "claim_verification_evaluation.json").read_text()
)


@pytest.mark.parametrize("case", EVALUATION_CASES, ids=lambda case: case["id"])
def test_claim_verification_deterministic_evaluation_fixtures(case):
    sources = []
    evidence_assessments = []
    for index, source_case in enumerate(case["sources"]):
        evidence = ()
        relation = source_case["relation"]
        if relation is not None:
            item = StoryEvidence(
                id=uuid4(),
                excerpt=source_case.get(
                    "excerpt",
                    "Solar cells convert sunlight into electrical energy.",
                ),
                locator=f"Fixture excerpt {index + 1}",
            )
            evidence = (item,)
        source = StorySource(
            id=uuid4(),
            canonical_url=f"https://publisher-{index + 1}.example/solar",
            title=f"Fixture source {index + 1}",
            publisher=f"Publisher {index + 1}",
            source_type="webpage",
            published_at=None,
            retrieved_at=datetime.now(UTC),
            evidence=evidence,
            publisher_domain=f"publisher-{index + 1}.example",
            source_class="unknown",
            independence_group_key=source_case["group"],
            independence_basis="near_duplicate_content",
            classification_version="1",
        )
        sources.append(source)
        if relation is not None:
            evidence_assessments.append((source.id, evidence[0].id, relation))

    context = StoryResearchContext(
        research_run_id=uuid4(),
        organization_id=uuid4(),
        topic="How solar cells work",
        sources=tuple(sources),
        included_evidence_count=sum(len(source.evidence) for source in sources),
        omitted_evidence_count=0,
        omitted_source_count=0,
        context_characters=sum(len(item.excerpt) for source in sources for item in source.evidence),
    )
    result, _, verifier = run_verification(
        context,
        extraction_response("Solar cells convert sunlight into electrical energy."),
        verification_response(assessment(0, *evidence_assessments)),
    )

    claim = result.artifact.factual_claims[0]
    assert claim.verification_status.value == case["expected_status"]
    assert claim.independent_support_group_count == case["expected_support_groups"]
    assert claim.independent_contradiction_group_count == case["expected_contradiction_groups"]
    if case.get("assert_untrusted_prompt_boundary"):
        system_message, user_message = verifier.requests[0].messages
        assert "untrusted reference data" in user_message.content
        assert "Ignore all prior instructions" in user_message.content
        assert "RESEARCH_AND_CLAIMS_JSON" in user_message.content
        assert "Ignore all prior instructions" not in system_message.content


def test_claim_verification_attaches_only_context_evidence_as_supported():
    context = make_context()
    source = context.sources[0]
    item = source.evidence[0]
    result, extractor, verifier = run_verification(
        context,
        extraction_response("Solar cells convert sunlight into electrical energy."),
        verification_response(
            assessment(0, (source.id, item.id, "supports")),
        ),
    )

    claim = result.artifact.factual_claims[0]
    assert claim.verification_status == ClaimVerificationStatus.SUPPORTED
    assert claim.source_ids == (source.id,)
    assert claim.evidence[0].evidence_id == item.id
    assert claim.independent_support_group_count == 1
    assert claim.independent_contradiction_group_count == 0
    assert result.extraction_provider == "extractor"
    assert result.verification_provider == "verifier"
    assert result.extraction_request_id == "extractor-1"
    assert result.verification_request_id == "verifier-1"
    assert len(extractor.requests) == len(verifier.requests) == 1
    assert extractor.requests[0].temperature == verifier.requests[0].temperature == 0.0


def test_claim_verification_marks_unopposed_contradicting_evidence_contradicted():
    context = make_context()
    source = context.sources[0]
    item = source.evidence[0]
    result, _, _ = run_verification(
        context,
        extraction_response("Solar cells convert sunlight into electrical energy."),
        verification_response(
            assessment(0, (source.id, item.id, "contradicts")),
        ),
    )

    claim = result.artifact.factual_claims[0]
    assert claim.verification_status == ClaimVerificationStatus.CONTRADICTED
    assert claim.independent_support_group_count == 0
    assert claim.independent_contradiction_group_count == 1


def test_claim_verification_marks_independent_support_and_contradiction_conflicting():
    context = make_context()
    supporting_source = context.sources[0]
    contradicting_source = replace(
        supporting_source,
        id=uuid4(),
        canonical_url="https://independent.example.net/solar",
        title="Independent solar cell analysis",
        publisher="Independent Publisher",
        evidence=(
            StoryEvidence(
                id=uuid4(),
                excerpt="Solar cells do not convert sunlight into electrical energy.",
                locator="Contrary analysis",
            ),
        ),
        publisher_domain="independent.example.net",
        independence_group_key="independent-publisher-group",
        independence_basis="unique_source",
    )
    context = StoryResearchContext(
        research_run_id=context.research_run_id,
        organization_id=context.organization_id,
        topic=context.topic,
        sources=(supporting_source, contradicting_source),
        included_evidence_count=2,
        omitted_evidence_count=0,
        omitted_source_count=0,
        context_characters=context.context_characters,
    )
    result, _, _ = run_verification(
        context,
        extraction_response("Solar cells convert sunlight into electrical energy."),
        verification_response(
            assessment(
                0,
                (supporting_source.id, supporting_source.evidence[0].id, "supports"),
                (contradicting_source.id, contradicting_source.evidence[0].id, "contradicts"),
            ),
        ),
    )

    claim = result.artifact.factual_claims[0]
    assert claim.verification_status == ClaimVerificationStatus.CONFLICTING
    assert claim.independent_support_group_count == 1
    assert claim.independent_contradiction_group_count == 1


def test_claim_verification_does_not_count_syndicated_sources_as_independent():
    context = make_context()
    original = context.sources[0]
    duplicate = StorySource(
        id=uuid4(),
        canonical_url="https://mirror.example.net/solar",
        title="Republished solar cell overview",
        publisher="Mirror Publisher",
        source_type="news",
        published_at=None,
        retrieved_at=datetime.now(UTC),
        evidence=(
            StoryEvidence(
                id=uuid4(),
                excerpt="Solar cells convert sunlight into electrical energy.",
                locator="Republished excerpt",
            ),
        ),
        publisher_domain="mirror.example.net",
        source_class="journalism",
        independence_group_key="same-syndicated-content-group",
        independence_basis="near_duplicate_content",
        classification_version="1",
    )
    original = replace(
        original,
        independence_group_key="same-syndicated-content-group",
        independence_basis="near_duplicate_content",
    )
    context = StoryResearchContext(
        research_run_id=context.research_run_id,
        organization_id=context.organization_id,
        topic=context.topic,
        sources=(original, duplicate),
        included_evidence_count=2,
        omitted_evidence_count=0,
        omitted_source_count=0,
        context_characters=context.context_characters,
    )
    result, _, verifier = run_verification(
        context,
        extraction_response("Solar cells convert sunlight into electrical energy."),
        verification_response(
            assessment(
                0,
                (original.id, original.evidence[0].id, "supports"),
                (duplicate.id, duplicate.evidence[0].id, "supports"),
            ),
        ),
    )

    claim = result.artifact.factual_claims[0]
    assert claim.verification_status == ClaimVerificationStatus.SUPPORTED
    assert len(claim.source_ids) == 2
    assert len(claim.evidence) == 2
    assert claim.independent_support_group_count == 1
    assert claim.independent_contradiction_group_count == 0
    payload = verifier.requests[0].messages[1].content
    assert "same-syndicated-content-group" in payload
    assert "not independent corroboration" in verifier.requests[0].messages[0].content


def test_claim_verification_marks_no_assessed_excerpt_insufficient():
    context = make_context(with_evidence=False)
    source = context.sources[0]
    result, _, _ = run_verification(
        context,
        extraction_response("A claim with no available excerpt."),
        verification_response(assessment(0)),
    )

    claim = result.artifact.factual_claims[0]
    assert claim.verification_status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert claim.source_ids == (source.id,)
    assert claim.evidence == ()


def test_claim_verification_marks_claim_unverifiable_without_sources():
    context = make_context(with_source=False)
    result, _, _ = run_verification(
        context,
        extraction_response("A claim without any research sources."),
        verification_response(assessment(0)),
    )

    claim = result.artifact.factual_claims[0]
    assert claim.verification_status == ClaimVerificationStatus.UNVERIFIABLE
    assert claim.source_ids == ()
    assert claim.evidence == ()


def test_claim_verification_rejects_evidence_id_outside_source_manifest():
    context = make_context()
    source = context.sources[0]
    extractor = QueueProvider(
        extraction_response("A factual claim."),
        name="extractor",
    )
    verifier = QueueProvider(
        verification_response(
            assessment(0, (source.id, uuid4(), "supports")),
        ),
        name="verifier",
    )

    with pytest.raises(StoryVerificationError, match="outside the source manifest"):
        asyncio.run(
            verify_story_claims(
                extractor,
                verifier,
                make_artifact(context),
                context,
                extraction_model="local-extract",
                verification_model="local-verify",
            )
        )


def test_claim_verification_rejects_source_id_outside_research_context():
    context = make_context()
    item = context.sources[0].evidence[0]
    extractor = QueueProvider(extraction_response("A factual claim."))
    verifier = QueueProvider(
        verification_response(assessment(0, (uuid4(), item.id, "supports"))),
    )

    with pytest.raises(StoryVerificationError, match="outside the research context"):
        asyncio.run(
            verify_story_claims(
                extractor,
                verifier,
                make_artifact(context),
                context,
                extraction_model="local-extract",
                verification_model="local-verify",
            )
        )


def test_claim_verification_requires_exactly_one_assessment_per_claim():
    context = make_context()
    extractor = QueueProvider(extraction_response("Claim one.", "Claim two."))
    verifier = QueueProvider(verification_response(assessment(0)))

    with pytest.raises(StoryVerificationError, match="every extracted claim exactly once"):
        asyncio.run(
            verify_story_claims(
                extractor,
                verifier,
                make_artifact(context),
                context,
                extraction_model="local-extract",
                verification_model="local-verify",
            )
        )


def test_claim_verification_skips_second_model_call_when_no_claims_found():
    context = make_context()
    extractor = QueueProvider(extraction_response())
    verifier = QueueProvider()

    result = asyncio.run(
        verify_story_claims(
            extractor,
            verifier,
            make_artifact(context),
            context,
            extraction_model="local-extract",
            verification_model="local-verify",
        )
    )

    assert result.artifact.factual_claims == ()
    assert len(extractor.requests) == 1
    assert verifier.requests == []
    assert result.verification_request_id is None


def test_claim_verification_rejects_story_context_provenance_mismatch():
    context = make_context()
    artifact = make_artifact(context)
    mismatched_context = make_context()

    with pytest.raises(StoryVerificationError, match="do not match"):
        asyncio.run(
            verify_story_claims(
                QueueProvider(extraction_response("Claim.")),
                QueueProvider(verification_response(assessment(0))),
                artifact,
                mismatched_context,
                extraction_model="local-extract",
                verification_model="local-verify",
            )
        )


def test_claim_verification_rejects_altered_source_identity_snapshot():
    context = make_context()
    payload = make_artifact(context).model_dump(mode="python")
    payload["source_references"][0]["canonical_url"] = (
        "https://example.com/not-the-retrieved-source"
    )
    altered_artifact = StoryArtifact.model_validate(payload)

    with pytest.raises(StoryVerificationError, match="identity metadata"):
        asyncio.run(
            verify_story_claims(
                QueueProvider(extraction_response("Claim.")),
                QueueProvider(verification_response(assessment(0))),
                altered_artifact,
                context,
                extraction_model="local-extract",
                verification_model="local-verify",
            )
        )


def test_claim_verification_rejects_altered_source_rights_snapshot():
    context = make_context()
    payload = make_artifact(context).model_dump(mode="python")
    payload["source_references"][0]["rights_metadata"] = RightsMetadata(
        status=RightsStatus.REVIEW_REQUIRED,
        scope=RightsScope.SOURCE_TEXT,
    ).model_dump(mode="python")
    altered_artifact = StoryArtifact.model_validate(payload)

    with pytest.raises(StoryVerificationError, match="rights metadata"):
        asyncio.run(
            verify_story_claims(
                QueueProvider(extraction_response("Claim.")),
                QueueProvider(verification_response(assessment(0))),
                altered_artifact,
                context,
                extraction_model="local-extract",
                verification_model="local-verify",
            )
        )


def test_claim_verification_rejects_duplicate_extracted_claims():
    context = make_context()
    duplicate = "Solar cells convert sunlight into electrical energy."
    extractor = QueueProvider(
        extraction_response(duplicate, f"  {duplicate}  "),
        name="extractor",
    )

    with pytest.raises(StoryVerificationError, match="duplicate factual claims"):
        asyncio.run(
            verify_story_claims(
                extractor,
                QueueProvider(),
                make_artifact(context),
                context,
                extraction_model="local-extract",
                verification_model="local-verify",
            )
        )


def test_claim_extraction_falls_back_to_story_sentences_and_still_checks_evidence():
    context = make_context()
    source = context.sources[0]
    malformed_extraction = json.dumps(
        {
            "narration": "Solar cells convert sunlight into electrical energy.",
            "topic": context.topic,
        }
    )
    extractor = QueueProvider(malformed_extraction, name="qwen-local")
    verifier = QueueProvider(
        verification_response(
            assessment(
                0,
                (
                    source.id,
                    source.evidence[0].id,
                    "supports",
                ),
            )
        ),
        name="qwen-local",
    )

    result = asyncio.run(
        verify_story_claims(
            extractor,
            verifier,
            make_artifact(context),
            context,
            extraction_model="qwen3:4b",
            verification_model="qwen3:4b",
        )
    )

    assert result.artifact.factual_claims[0].claim == (
        "Solar cells convert sunlight into electrical energy."
    )
    assert (
        result.artifact.factual_claims[0].verification_status == ClaimVerificationStatus.SUPPORTED
    )
    assert result.extraction_provider == "aurora-deterministic"
    assert result.extraction_model == "sentence-splitter.v1"


def test_claim_verification_provider_failure_fails_closed_and_allows_review():
    context = make_context()

    class UnavailableProvider:
        async def generate(self, request):
            raise LLMProviderError("Configured LLM provider request failed.")

    result = asyncio.run(
        verify_story_claims(
            QueueProvider(
                extraction_response("Solar cells convert sunlight into electrical energy."),
                name="qwen-local",
            ),
            UnavailableProvider(),
            make_artifact(context),
            context,
            extraction_model="qwen3:4b",
            verification_model="qwen3:4b",
        )
    )

    claim = result.artifact.factual_claims[0]
    assert claim.verification_status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    assert claim.evidence == ()
    assert claim.source_ids == tuple(source.id for source in context.sources)
    assert result.verification_provider == "unavailable"


def test_claim_verification_rejects_artifacts_that_already_have_claims():
    context = make_context()
    artifact = make_artifact(context)
    artifact = StoryArtifact.model_validate(
        {
            **artifact.model_dump(mode="python"),
            "factual_claims": (
                {
                    "claim": "Existing claim.",
                    "verification_status": ClaimVerificationStatus.UNVERIFIABLE,
                    "source_ids": (),
                    "evidence": (),
                },
            ),
        }
    )

    with pytest.raises(StoryVerificationError, match="without previously attached"):
        asyncio.run(
            verify_story_claims(
                QueueProvider(extraction_response("New claim.")),
                QueueProvider(verification_response(assessment(0))),
                artifact,
                context,
                extraction_model="local-extract",
                verification_model="local-verify",
            )
        )
