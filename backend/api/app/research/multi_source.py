"""Deterministic composition of independent factual research adapters."""
from __future__ import annotations

import asyncio

from app.research.contracts import ResearchDocument, ResearchProvider
from app.research.normalization import content_sha256, normalize_source_url

_MAX_PROVIDERS = 5
_MAX_DOCUMENTS = 20


class MultiSourceResearchProvider:
    """Query configured research providers concurrently and reconcile duplicates.

    A provider error fails the composed retrieval rather than silently degrading
    a configured multi-source run. Empty provider results are allowed because
    some scholarly topics have no indexed abstract. Identical canonical
    documents are deduplicated; conflicting content for one URL is rejected.
    """

    def __init__(self, providers: tuple[ResearchProvider, ...]) -> None:
        self._providers = tuple(providers)
        if not 2 <= len(self._providers) <= _MAX_PROVIDERS:
            raise ValueError(
                f"Configure between 2 and {_MAX_PROVIDERS} research providers."
            )

    async def research(self, topic: str) -> tuple[ResearchDocument, ...]:
        normalized_topic = topic.strip()
        if not normalized_topic:
            raise ValueError("Research topic must not be empty.")

        # Wait for every provider to stop before failing the composed request.
        # This avoids leaving a sibling network task running after one source
        # fails, while refusing to present partial results as complete research.
        provider_results = await asyncio.gather(
            *(provider.research(normalized_topic) for provider in self._providers),
            return_exceptions=True,
        )
        for result in provider_results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                raise RuntimeError(
                    "A configured research provider failed."
                ) from result

        documents: list[ResearchDocument] = []
        by_url: dict[str, str] = {}
        for result in provider_results:
            for document in result:
                if not isinstance(document, ResearchDocument):
                    raise TypeError("Research providers must return ResearchDocument values.")
                canonical_url = normalize_source_url(document.canonical_url)
                content_hash = content_sha256(document.content)
                previous_hash = by_url.get(canonical_url)
                if previous_hash is not None:
                    if previous_hash != content_hash:
                        raise ValueError(
                            "Research providers returned conflicting content for one source URL."
                        )
                    continue
                by_url[canonical_url] = content_hash
                documents.append(document)
                if len(documents) > _MAX_DOCUMENTS:
                    raise ValueError(
                        f"Composed research returned more than {_MAX_DOCUMENTS} documents."
                    )

        return tuple(documents)
