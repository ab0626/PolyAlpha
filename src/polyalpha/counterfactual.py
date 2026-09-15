"""Counterfactual analysis — what-if for accepted/rejected signals.

Section 89A-B of the v0.3 spec: analyze what would have happened if
we had taken (or not taken) each signal.
"""

from dataclasses import dataclass
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class CounterfactualResult:
    """What-if analysis for a single decision."""

    market_id: str
    actual_action: str  # "accepted" or "rejected"
    counterfactual_action: str  # what we're analyzing
    actual_pnl: Decimal
    counterfactual_pnl: Decimal  # estimated PnL if we had done the opposite
    regret: Decimal  # difference (opportunity cost)
    confidence: float  # how confident in the estimate

    def summary(self) -> dict:
        return {
            "market_id": self.market_id,
            "actual_action": self.actual_action,
            "counterfactual_action": self.counterfactual_action,
            "actual_pnl": str(self.actual_pnl),
            "counterfactual_pnl": str(self.counterfactual_pnl),
            "regret": str(self.regret),
        }


def analyze_counterfactuals(
    decisions: list[dict],
    outcomes: dict[str, float],
) -> list[CounterfactualResult]:
    """Analyze counterfactuals for a set of decisions.

    Args:
        decisions: list of dicts with market_id, action, entry_price, probability, pnl
        outcomes: dict of market_id -> actual outcome (0 or 1)
    """
    results = []
    for dec in decisions:
        market_id = dec["market_id"]
        action = dec.get("action", "accepted")
        entry_price = dec.get("entry_price", 0.5)
        dec.get("probability", 0.5)
        actual_pnl = D(str(dec.get("pnl", 0)))
        outcome = outcomes.get(market_id)

        if outcome is None:
            continue

        # Counterfactual: what if we did the opposite?
        if action == "accepted":
            # We bought — what if we hadn't?
            cf_pnl = -actual_pnl  # approximate: opportunity cost
            cf_action = "rejected"
        else:
            # We didn't buy — what if we had?
            if outcome == 1:
                cf_pnl = D(str(1.0 - entry_price)) * D("100")  # approximate
            else:
                cf_pnl = -D(str(entry_price)) * D("100")
            cf_action = "accepted"

        regret = cf_pnl - actual_pnl

        results.append(
            CounterfactualResult(
                market_id=market_id,
                actual_action=action,
                counterfactual_action=cf_action,
                actual_pnl=actual_pnl,
                counterfactual_pnl=cf_pnl,
                regret=regret,
                confidence=0.5,  # moderate confidence in counterfactual estimate
            )
        )

    return results
