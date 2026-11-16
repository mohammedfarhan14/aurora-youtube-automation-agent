from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.rights.contracts import RightsMetadata


@dataclass(frozen=True, slots=True)
class EvidenceExcerpt:
    excerpt: str
    locator: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchDocument:
    canonical_url: str
    title: str
    publisher: str | None
    source_type: str
    published_at: datetime | None
    retrieved_at: datetime
    content: str
    evidence: tuple[EvidenceExcerpt, ...]
    rights_metadata: RightsMetadata = field(default_factory=RightsMetadata)


class ResearchProvider(Protocol):
    """Provider-neutral interface for retrieving research material."""

    async def research(self, topic: str) -> Sequence[ResearchDocument]:
        """Return source documents and their evidence excerpts."""
        ...
