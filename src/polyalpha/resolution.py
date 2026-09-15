"""Human-reviewed resolution semantics; changed wording invalidates the assessment.

Enhanced with temporal decay: older reviews receive increasing penalty
to encourage periodic re-review of resolution quality.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .domain import utc

D = Decimal


def definition_hash(market):
    definition = dict(
        question=market.question,
        description=market.description,
        resolution_source=market.resolution_source,
        deadline=market.deadline.isoformat() if market.deadline else None,
    )
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ResolutionReview:
    """Human-reviewed resolution quality assessment.

    Attributes:
        definition_sha256: Hash of the market definition at review time
        reviewed_at: When the review was conducted
        reference: Source/reference for the review
        clarity: 0-1 score for resolution clarity
        ambiguity: 0-1 score for ambiguity in resolution criteria
        dispute_risk: 0-1 score for likelihood of dispute
    """

    definition_sha256: str
    reviewed_at: datetime
    reference: str
    clarity: Decimal
    ambiguity: Decimal
    dispute_risk: Decimal

    def __post_init__(self):
        utc(self.reviewed_at)
        if not self.reference or len(self.definition_sha256) != 64:
            raise ValueError("resolution review provenance required")
        if any(
            not x.is_finite() or not 0 <= x <= 1
            for x in (self.clarity, self.ambiguity, self.dispute_risk)
        ):
            raise ValueError("invalid resolution assessment")

    def penalty(self, market, at, review_decay_days: float = 90) -> Decimal:
        """Compute resolution risk penalty.

        Base penalty = 0.01 + 0.05 * (ambiguity + dispute_risk)

        Temporal decay: for each review_decay_days since review,
        add an additional penalty of 0.005, capped at 0.03.

        This encourages periodic re-review of resolution quality.

        Args:
            market: Market object to check definition against
            at: Current timestamp
            review_decay_days: Days after which review freshness decays
        """
        if self.reviewed_at > at or self.definition_sha256 != definition_hash(market):
            raise ValueError("resolution review unavailable or definition changed")
        if self.clarity < D(".8"):
            raise ValueError("resolution clarity insufficient")

        base = D(".01") + D(".05") * (self.ambiguity + self.dispute_risk)

        # Temporal decay: older reviews get higher penalty
        age_days = (at - self.reviewed_at).total_seconds() / 86400
        decay_periods = max(0, int(age_days / review_decay_days))
        temporal_penalty = min(D(".03"), D("0.005") * decay_periods)

        return base + temporal_penalty

    @classmethod
    def from_dict(cls, raw):
        return cls(
            raw["definition_sha256"],
            datetime.fromisoformat(raw["reviewed_at"]),
            raw["reference"],
            Decimal(str(raw["clarity"])),
            Decimal(str(raw["ambiguity"])),
            Decimal(str(raw["dispute_risk"])),
        )


@dataclass(frozen=True)
class ResolutionQualityScore:
    """Automated resolution quality score based on market metadata.

    Provides a heuristic score without requiring human review.
    Useful as a filter before human review or when reviews are unavailable.
    """

    clarity_score: Decimal
    source_reliability: Decimal
    deadline_present: bool
    objective_criteria: bool
    definition_length_penalty: Decimal

    @classmethod
    def from_market(cls, market) -> "ResolutionQualityScore":
        """Compute resolution quality from market metadata."""
        # Clarity heuristics
        question_len = len(market.question or "")
        desc_len = len(market.description or "")

        # Short questions are often clearer
        clarity = D("1.0")
        if question_len > 200:
            clarity -= D("0.1")
        if desc_len > 1000:
            clarity -= D("0.1")
        if not market.description:
            clarity -= D("0.2")

        # Source reliability
        source = (market.resolution_source or "").lower()
        if any(kw in source for kw in ("official", "government", "polymarket")):
            source_rel = D("0.9")
        elif any(kw in source for kw in ("news", "reuters", "ap", "bloomberg")):
            source_rel = D("0.7")
        elif source:
            source_rel = D("0.5")
        else:
            source_rel = D("0.3")

        deadline_present = market.deadline is not None

        # Heuristic for objective vs subjective criteria
        subjective_words = ("best", "most", "should", "will", "opinion", "feel")
        objective_words = ("number", "percentage", "above", "below", "date", "time")
        q_lower = (market.question or "").lower()
        has_subjective = any(w in q_lower for w in subjective_words)
        has_objective = any(w in q_lower for w in objective_words)
        objective = has_objective and not has_subjective

        # Definition length penalty: very short definitions may lack detail
        def_len = desc_len
        length_penalty = D(0)
        if def_len < 50:
            length_penalty = D("0.1")

        return cls(
            clarity_score=clarity,
            source_reliability=source_rel,
            deadline_present=deadline_present,
            objective_criteria=objective,
            definition_length_penalty=length_penalty,
        )

    @property
    def overall_score(self) -> Decimal:
        """Weighted overall resolution quality score."""
        score = (
            self.clarity_score * D("0.4")
            + self.source_reliability * D("0.3")
            + (D("0.15") if self.deadline_present else D(0))
            + (D("0.15") if self.objective_criteria else D(0))
            - self.definition_length_penalty
        )
        return max(D(0), min(D(1), score))

    @property
    def resolution_penalty(self) -> Decimal:
        """Penalty derived from quality score for net-edge calculation."""
        # Lower quality = higher penalty
        return D("0.01") + D("0.05") * (D(1) - self.overall_score)
