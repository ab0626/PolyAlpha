"""Manually reviewed logical constraints; diagnostics are not executable arbitrage.

Constraint types (Section 13 edge types):
- partition: Markets are mutually exclusive and exhaustive (sum = 1)
- exclusive: Markets are mutually exclusive (sum ≤ 1)
- implication: Market A implies market B (if A then B) → P(A) ≤ P(B)
- complement: Two markets that complement each other (A + B = 1)
- subsume: Market A subsumes market B (P(A) ≥ P(B))
- threshold: Probability bound on a market (P(A) ≤ bound)
"""

from dataclasses import dataclass
from decimal import Decimal

CONSTRAINT_TYPES = frozenset(
    {
        "partition",
        "exclusive",
        "implication",
        "complement",
        "subsume",
        "threshold",
    }
)


@dataclass(frozen=True)
class Constraint:
    kind: str
    markets: tuple[str, ...]
    review_reference: str
    reviewed_at: object
    threshold: Decimal | None = None  # only for kind="threshold"

    def __post_init__(self):
        from .domain import utc

        utc(self.reviewed_at)
        if self.kind not in CONSTRAINT_TYPES or not self.review_reference:
            raise ValueError("reviewed constraint required")
        if len(set(self.markets)) != len(self.markets):
            raise ValueError("duplicate members")
        if self.kind == "threshold":
            if len(self.markets) != 1:
                raise ValueError("threshold requires exactly one market")
            if self.threshold is None or not (0 <= self.threshold <= 1):
                raise ValueError("threshold must be in [0,1]")
        elif self.kind in ("implication", "complement", "subsume"):
            if len(self.markets) != 2:
                raise ValueError(f"{self.kind} requires two members")
        else:
            if len(self.markets) < 2:
                raise ValueError("constraint requires at least two markets")


def violations(constraints, probabilities, at):
    result = []
    for rule in constraints:
        if rule.reviewed_at > at:
            continue
        if any(m not in probabilities for m in rule.markets):
            continue
        values = [probabilities[m] for m in rule.markets]
        if any(not p.is_finite() or not 0 <= p <= 1 for p in values):
            raise ValueError("invalid probabilities")

        if rule.kind == "partition":
            violation = abs(sum(values) - 1)
        elif rule.kind == "exclusive":
            violation = max(Decimal(0), sum(values) - 1)
        elif rule.kind == "implication":
            # If A then B → P(A) ≤ P(B), violation = P(A) - P(B)
            violation = max(Decimal(0), values[0] - values[1])
        elif rule.kind == "complement":
            # A + B = 1
            violation = abs(sum(values) - 1)
        elif rule.kind == "subsume":
            # A subsumes B → P(A) ≥ P(B), violation = P(B) - P(A)
            violation = max(Decimal(0), values[1] - values[0])
        elif rule.kind == "threshold":
            violation = max(Decimal(0), values[0] - rule.threshold)
        else:
            continue

        if violation:
            result.append(
                dict(
                    kind=rule.kind,
                    markets=rule.markets,
                    magnitude=str(violation),
                    review=rule.review_reference,
                )
            )
    return result
