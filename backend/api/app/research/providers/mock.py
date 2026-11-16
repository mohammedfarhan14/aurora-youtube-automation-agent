from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import quote

from app.research.contracts import (
    EvidenceExcerpt,
    ResearchDocument,
)


class MockResearchProvider:
    """Deterministic provider returning explicitly synthetic research material."""

    async def research(self, topic: str) -> tuple[ResearchDocument, ...]:
        normalized_topic = topic.strip()

        if not normalized_topic:
            raise ValueError("Research topic must not be empty.")

        topic_hash = sha256(
            normalized_topic.casefold().encode("utf-8")
        ).hexdigest()[:16]

        topic_path = quote(normalized_topic, safe="")

        overview_content = (
            f"Synthetic mock research material about {normalized_topic}. "
            "This content is generated locally for development and testing. "
            "It is not a real-world source or factual evidence."
        )

        context_content = (
            f"Synthetic contextual material about {normalized_topic}. "
            "This content is generated locally for development and testing. "
            "It must not be presented as verified research."
        )

        return (
            ResearchDocument(
                canonical_url=(
                    f"https://example.com/mock-research/{topic_hash}/overview"
                    f"?topic={topic_path}"
                ),
                title=f"Synthetic research overview: {normalized_topic}",
                publisher="Aurora Studio Mock Provider",
                source_type="other",
                published_at=None,
                retrieved_at=datetime.now(UTC),
                content=overview_content,
                evidence=(
                    EvidenceExcerpt(
                        excerpt=overview_content,
                        locator="synthetic overview",
                    ),
                ),
            ),
            ResearchDocument(
                canonical_url=(
                    f"https://example.com/mock-research/{topic_hash}/context"
                    f"?topic={topic_path}"
                ),
                title=f"Synthetic research context: {normalized_topic}",
                publisher="Aurora Studio Mock Provider",
                source_type="other",
                published_at=None,
                retrieved_at=datetime.now(UTC),
                content=context_content,
                evidence=(
                    EvidenceExcerpt(
                        excerpt=context_content,
                        locator="synthetic context",
                    ),
                ),
            ),
        )
