"""News and information pipeline for probability impact estimation.

Pipeline: news/data source → deduplication → timestamp normalization →
entity extraction → event matching → claim extraction →
probability-impact model → updated probability estimate → market comparison

Every external fact carries full provenance metadata.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .domain import utc

D = Decimal


@dataclass(frozen=True)
class ExtractedEntity:
    """An entity extracted from external information."""

    entity_type: str  # person, organization, event, location
    name: str
    confidence: Decimal  # extraction confidence 0-1

    def __post_init__(self):
        if not self.name:
            raise ValueError("entity name required")
        if not (0 <= self.confidence <= 1):
            raise ValueError("confidence must be in [0,1]")


@dataclass(frozen=True)
class ClaimExtraction:
    """A claim extracted from external information with probability impact."""

    claim_text: str
    source_url: str
    published_at: datetime
    retrieved_at: datetime
    entities: tuple[ExtractedEntity, ...]
    probability_impact: Decimal  # estimated delta: positive = increases YES probability
    impact_confidence: Decimal  # confidence in the impact estimate 0-1
    category: str  # politics, crypto, sports, etc.
    content_hash: str  # exact text+source key (NOT semantic)
    claim_id: str | None = None  # source-independent semantic claim key

    def __post_init__(self):
        utc(self.published_at)
        utc(self.retrieved_at)
        if not self.claim_text:
            raise ValueError("claim text required")


class ProbabilityImpactModel(Protocol):
    """Estimate probability impact from external information."""

    def estimate_impact(
        self,
        claim: ClaimExtraction,
        current_probability: Decimal,
        market_features: dict,
    ) -> Decimal:
        """Return estimated probability delta.

        Positive = event becomes more likely.
        """
        ...


class SimpleImpactModel:
    """Stub impact model for infrastructure testing.

    Returns a configurable default impact. Replace with a real model
    trained on historical information-probability-price triples.
    """

    def __init__(self, default_impact: Decimal = D("0.02"), max_impact: Decimal = D("0.10")):
        self.default_impact = default_impact
        self.max_impact = max_impact

    def estimate_impact(self, claim, current_probability, market_features=None):
        # Infrastructure placeholder: return small default impact
        # Real implementation should:
        # 1. Parse claim semantics by category
        # 2. Match entities to market question
        # 3. Estimate direction and magnitude
        # 4. Adjust for source reliability
        return min(self.default_impact, self.max_impact)


class ClaimDeduplicator:
    """Deduplicate claims by content hash with time window."""

    def __init__(self, window_seconds: int = 3600):
        self.window_seconds = window_seconds
        self.seen: dict[str, datetime] = {}

    def is_duplicate(self, claim: ClaimExtraction) -> bool:
        """Check if claim is a near-duplicate within the time window.

        Dedupes on the *semantic* claim_id when present (so Reuters and CNBC
        carrying the same shock collapse to one observation), falling back to
        the exact text+source content_hash.
        """
        h = claim.claim_id or claim.content_hash
        if h in self.seen:
            existing = self.seen[h]
            delta = (claim.retrieved_at - existing).total_seconds()
            if delta < self.window_seconds:
                return True
        self.seen[h] = claim.retrieved_at
        return False

    def cleanup(self, before: datetime):
        """Remove entries older than the window."""
        cutoff = before.timestamp() - self.window_seconds
        self.seen = {h: t for h, t in self.seen.items() if t.timestamp() >= cutoff}


def compute_content_hash(text: str, source_url: str) -> str:
    """Compute deterministic content hash for deduplication."""
    content = f"{text.strip().lower()}|{source_url.strip().lower()}"
    return hashlib.sha256(content.encode()).hexdigest()


def compute_claim_id(entities: tuple[ExtractedEntity, ...], claim_text: str) -> str:
    """Source-independent semantic claim key.

    The same underlying shock reported by two outlets should share a claim_id,
    unlike content_hash (which keys on text + source URL). Canonicalizes to
    (sorted entity names + whitespace-normalized text). Full cross-phrasing
    similarity (Reuters vs CNBC wording) requires an embedding model, noted as
    future work.
    """
    names = ",".join(sorted(e.name.strip().lower() for e in entities))
    text = " ".join(claim_text.strip().lower().split())
    return hashlib.sha256(f"{names}|{text}".encode("utf-8")).hexdigest()


@dataclass
class InformationPipelineState:
    """State for the information pipeline."""

    deduplicator: ClaimDeduplicator = field(default_factory=ClaimDeduplicator)
    claims_processed: int = 0
    claims_deduplicated: int = 0
    impacts_computed: int = 0


class InformationPipeline:
    """Process external information into probability impacts.

    Orchestrates: deduplication → impact estimation → provenance tracking.
    """

    def __init__(
        self,
        impact_model: ProbabilityImpactModel | None = None,
        dedup_window_seconds: int = 3600,
    ):
        self.impact_model = impact_model or SimpleImpactModel()
        self.state = InformationPipelineState(deduplicator=ClaimDeduplicator(dedup_window_seconds))

    def process_claim(
        self,
        claim: ClaimExtraction,
        current_probability: Decimal,
        market_features: dict | None = None,
    ) -> dict | None:
        """Process a single claim through the pipeline.

        Returns None if claim is a duplicate.
        Returns dict with impact analysis otherwise.
        """
        self.state.claims_processed += 1

        # Deduplication
        if self.state.deduplicator.is_duplicate(claim):
            self.state.claims_deduplicated += 1
            return None

        # Impact estimation
        impact = self.impact_model.estimate_impact(
            claim, current_probability, market_features or {}
        )
        self.state.impacts_computed += 1

        new_probability = max(D(0), min(D(1), current_probability + impact))

        return dict(
            content_hash=claim.content_hash,
            source_url=claim.source_url,
            published_at=claim.published_at.isoformat(),
            retrieved_at=claim.retrieved_at.isoformat(),
            category=claim.category,
            entity_count=len(claim.entities),
            claim_length=len(claim.claim_text),
            impact=str(impact),
            impact_confidence=str(claim.impact_confidence),
            old_probability=str(current_probability),
            new_probability=str(new_probability),
            delta=str(new_probability - current_probability),
        )

    def process_batch(
        self,
        claims: list[ClaimExtraction],
        current_probability: Decimal,
        market_features: dict | None = None,
    ) -> list[dict]:
        """Process multiple claims, returning non-duplicate impacts."""
        results = []
        for claim in sorted(claims, key=lambda c: c.published_at):
            result = self.process_claim(claim, current_probability, market_features)
            if result is not None:
                results.append(result)
                # Update probability for next claim in sequence
                current_probability = D(result["new_probability"])
        return results

    def summary(self) -> dict:
        return dict(
            claims_processed=self.state.claims_processed,
            claims_deduplicated=self.state.claims_deduplicated,
            claims_unique=self.state.claims_processed - self.state.claims_deduplicated,
            impacts_computed=self.state.impacts_computed,
        )
