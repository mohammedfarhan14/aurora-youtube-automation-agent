"""Deterministic source provenance and independence assessment.

This module records structural provenance, not a truth or trust score. Domain
classification is deliberately conservative and must not be interpreted as a
claim that a publisher is authoritative for a particular fact.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

from app.research.contracts import ResearchDocument
from app.research.normalization import content_sha256, normalize_source_url

_MAX_DOCUMENTS = 20
_MAX_CONTENT_CHARS = 100_000
_NEAR_DUPLICATE_THRESHOLD = 0.80
_MIN_SHINGLES = 5
_MAX_TOTAL_CONTENT_CHARS = 1_000_000
_CLASSIFICATION_VERSION = "1"


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    publisher_domain: str
    source_class: str
    independence_group_key: str
    independence_basis: str
    classification_version: str = _CLASSIFICATION_VERSION


def _publisher_domain(url: str) -> str:
    hostname = urlsplit(url).hostname
    if not hostname:
        raise ValueError("Research source URL has no publisher hostname.")
    hostname = hostname.rstrip(".").casefold()
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Research source hostname is invalid.") from exc
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname or len(hostname) > 255:
        raise ValueError("Research source publisher hostname is invalid.")
    return hostname


def _source_class(source_type: str, hostname: str) -> str:
    if source_type == "paper":
        return "scholarly"
    if source_type == "news":
        return "journalism"
    if source_type == "video":
        return "video"
    if hostname == "wikipedia.org" or hostname.endswith(".wikipedia.org"):
        return "reference"
    if hostname == "reddit.com" or hostname.endswith(".reddit.com"):
        return "community"
    if (
        hostname.endswith(".gov")
        or hostname.endswith(".gov.uk")
        or hostname.endswith(".gov.in")
    ):
        return "official_domain"
    return "unknown"


def _shingles(content: str) -> frozenset[tuple[str, ...]]:
    tokens = re.findall(r"[^\W_]+", content.casefold(), flags=re.UNICODE)
    if len(tokens) < 5:
        return frozenset()
    return frozenset(tuple(tokens[index:index + 5]) for index in range(len(tokens) - 4))


def _near_duplicate(
    left: frozenset[tuple[str, ...]],
    right: frozenset[tuple[str, ...]],
) -> bool:
    if len(left) < _MIN_SHINGLES or len(right) < _MIN_SHINGLES:
        return False
    intersection = len(left & right)
    union = len(left | right)
    return bool(union) and intersection / union >= _NEAR_DUPLICATE_THRESHOLD


def assess_source_provenance(
    documents: Sequence[ResearchDocument],
) -> tuple[SourceProvenance, ...]:
    """Return stable publisher and independence metadata in input order.

    Sources are grouped when they share a normalized publisher hostname or
    when their retrieved text is an exact/near duplicate. We intentionally do
    not infer registrable domains without a Public Suffix List: subdomains may
    be distinct publishers, so the hostname grouping is conservative.
    """

    if len(documents) > _MAX_DOCUMENTS:
        raise ValueError(f"At most {_MAX_DOCUMENTS} sources may be assessed.")

    total_content_chars = sum(len(document.content) for document in documents)
    if total_content_chars > _MAX_TOTAL_CONTENT_CHARS:
        raise ValueError("Research source set exceeds the total provenance assessment limit.")

    normalized_urls: list[str] = []
    domains: list[str] = []
    classes: list[str] = []
    hashes: list[str] = []
    shingles: list[frozenset[tuple[str, ...]]] = []

    for document in documents:
        if len(document.content) > _MAX_CONTENT_CHARS:
            raise ValueError("Research source content exceeds the provenance assessment limit.")
        canonical_url = normalize_source_url(document.canonical_url)
        domain = _publisher_domain(canonical_url)
        normalized_urls.append(canonical_url)
        domains.append(domain)
        classes.append(_source_class(document.source_type, domain))
        hashes.append(content_sha256(document.content))
        shingles.append(_shingles(document.content))

    parent = list(range(len(documents)))
    group_reasons: list[set[str]] = [set() for _ in documents]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int, reason: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root
        group_reasons[left].add(reason)
        group_reasons[right].add(reason)

    for left in range(len(documents)):
        for right in range(left + 1, len(documents)):
            if domains[left] == domains[right]:
                union(left, right, "publisher_domain")
            elif hashes[left] == hashes[right]:
                union(left, right, "exact_content")
            elif _near_duplicate(shingles[left], shingles[right]):
                union(left, right, "near_duplicate_content")

    groups: dict[int, list[int]] = {}
    for index in range(len(documents)):
        groups.setdefault(find(index), []).append(index)

    results: list[SourceProvenance | None] = [None] * len(documents)
    for indices in groups.values():
        stable_members = sorted(normalized_urls[index] for index in indices)
        group_key = sha256("\n".join(stable_members).encode("utf-8")).hexdigest()
        reasons = set().union(*(group_reasons[index] for index in indices))
        if len(indices) == 1:
            basis = "unique_source"
        elif "exact_content" in reasons:
            basis = "exact_content"
        elif "near_duplicate_content" in reasons:
            basis = "near_duplicate_content"
        else:
            basis = "publisher_domain"
        for index in indices:
            results[index] = SourceProvenance(
                publisher_domain=domains[index],
                source_class=classes[index],
                independence_group_key=group_key,
                independence_basis=basis,
            )

    return tuple(item for item in results if item is not None)
