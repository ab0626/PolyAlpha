"""Signal review classification — REQUIRE_REVIEW, ACTIONABLE, or REJECT.

Part 76: Classifies signals based on model-market disagreement and ambiguity
to determine whether human review is required before execution.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClassificationResult:
    """Result of signal classification."""

    classification: str  # "REQUIRE_REVIEW", "ACTIONABLE", "REJECT"
    reason: str
    model_prob: float
    market_prob: float
    ambiguity_score: float
    disagreement: float

    def summary(self) -> dict:
        return {
            "classification": self.classification,
            "reason": self.reason,
            "model_prob": self.model_prob,
            "market_prob": self.market_prob,
            "ambiguity_score": self.ambiguity_score,
            "disagreement": self.disagreement,
        }


def classify_signal(
    model_prob: float,
    market_prob: float,
    ambiguity_score: float,
    disagreement_threshold: float = 0.15,
    ambiguity_threshold: float = 0.7,
) -> ClassificationResult:
    """Classify a signal as ACTIONABLE, REQUIRE_REVIEW, or REJECT.

    Rules:
    - If |model - market| > disagreement_threshold AND ambiguity > ambiguity_threshold
      → REQUIRE_REVIEW (signal needs human judgment)
    - If model_prob is too close to 0.5 or market is too far from model
      → REJECT (signal is not actionable)
    - Otherwise → ACTIONABLE (signal can be executed automatically)

    Args:
        model_prob: Model's predicted probability.
        market_prob: Current market mid probability.
        ambiguity_score: Ambiguity score [0, 1] — higher = more ambiguous.
        disagreement_threshold: Minimum |model - market| for disagreement flag.
        ambiguity_threshold: Minimum ambiguity score for review flag.

    Returns:
        ClassificationResult with classification string and reason.
    """
    disagreement = abs(model_prob - market_prob)

    # REQUIRE_REVIEW: high disagreement AND high ambiguity
    if disagreement > disagreement_threshold and ambiguity_score > ambiguity_threshold:
        return ClassificationResult(
            classification="REQUIRE_REVIEW",
            reason=(
                f"High disagreement ({disagreement:.3f} > {disagreement_threshold}) "
                f"combined with high ambiguity ({ambiguity_score:.3f} > {ambiguity_threshold}). "
                f"Model says {model_prob:.3f}, market says {market_prob:.3f}."
            ),
            model_prob=model_prob,
            market_prob=market_prob,
            ambiguity_score=ambiguity_score,
            disagreement=disagreement,
        )

    # REJECT: low edge or contradictory signals with no clarity
    edge = abs(model_prob - 0.5)
    if edge < 0.05:
        return ClassificationResult(
            classification="REJECT",
            reason=(
                f"Model probability {model_prob:.3f} too close to 0.5 — "
                f"no meaningful edge detected."
            ),
            model_prob=model_prob,
            market_prob=market_prob,
            ambiguity_score=ambiguity_score,
            disagreement=disagreement,
        )

    if disagreement > 0.30:
        return ClassificationResult(
            classification="REJECT",
            reason=(
                f"Extreme disagreement ({disagreement:.3f}) suggests model or "
                f"market mispricing. Too risky for automated execution."
            ),
            model_prob=model_prob,
            market_prob=market_prob,
            ambiguity_score=ambiguity_score,
            disagreement=disagreement,
        )

    # ACTIONABLE: reasonable edge, manageable disagreement
    return ClassificationResult(
        classification="ACTIONABLE",
        reason=(
            f"Edge {edge:.3f} with disagreement {disagreement:.3f} "
            f"within acceptable bounds."
        ),
        model_prob=model_prob,
        market_prob=market_prob,
        ambiguity_score=ambiguity_score,
        disagreement=disagreement,
    )
