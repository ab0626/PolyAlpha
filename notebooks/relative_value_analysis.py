"""Relative Value Analysis Notebook.

Analyzes cross-market relationships:
- Constraint graph construction
- Partition arbitrage detection
- Event cluster exposure analysis
- Correlated market detection

Usage:
    python -m notebooks.relative_value_analysis
"""

# %%
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from datetime import UTC, datetime
from decimal import Decimal

D = Decimal

# %%
# Example: Partition constraint analysis
print("Relative Value Analysis")
print("=" * 50)

from polyalpha.graph import ConstraintGraph, partition
from polyalpha.relative_value import Constraint

# %%
# Simulate a set of mutually exclusive outcomes
# (In production, these come from real market data)
print("\n--- Partition Arbitrage Detection ---")

example_prices = {
    "candidate_A_wins": D("0.45"),
    "candidate_B_wins": D("0.32"),
    "candidate_C_wins": D("0.17"),
    "no_winner": D("0.08"),
}

total = sum(example_prices.values())
print(f"\nOutcome prices:")
for name, price in example_prices.items():
    print(f"  {name:25s}: {price}")
print(f"\n  Sum of YES prices: {total:.3f}")
print(f"  Sum - 1.0:        {total - 1:.3f}")
print(f"  Overround:         {(1 - total):.3f}")

if total < D("0.95"):
    print(f"\n  *** Potential arbitrage: sum={total:.3f} < 0.95 ***")
    print(f"  (After fees and slippage, this may or may not be real)")
elif total > D("1.05"):
    print(f"\n  *** Overpriced basket: sum={total:.3f} > 1.05 ***")
    print(f"  (NO side of basket may be attractive)")

# %%
# Constraint types demonstration
print("\n--- Constraint Types ---")

from polyalpha.domain import Market
from polyalpha.resolution import ResolutionReview

now = datetime.now(UTC)
example_markets = []
for name in example_prices:
    m = Market(
        market_id=name,
        condition_id=name,
        event_ids=("election_2028",),
        question=f"Does {name.replace('_', ' ')}?",
        description="Example",
        resolution_source="test",
        deadline=now,
        active=True,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        liquidity=D(10000),
        volume=D(5000),
        fees_enabled=False,
        fee_parameters_json=None,
        yes_token_id=f"tok_{name}",
        no_token_id=f"tok_{name}_no",
        received_at=now,
        category="politics",
    )
    example_markets.append(m)

# Build constraint graph
from polyalpha.graph import ReviewedNode

nodes = []
reviews = {}
for m in example_markets:
    review = ResolutionReview(
        market_id=m.market_id,
        definition_hash="example",
        clarity_score=D("0.8"),
        ambiguity_score=D("0.2"),
        dispute_risk_score=D("0.1"),
        reviewed_at=now,
        reviewer="notebook",
    )
    nodes.append(ReviewedNode(m, review))
    reviews[m.market_id] = review

# Create partition rule
rules = [
    Constraint(
        kind="partition",
        markets=tuple(m.market_id for m in example_markets),
        review_reference="partition_analysis",
        reviewed_at=now,
    )
]

graph = ConstraintGraph(nodes, rules)
reports = graph.check_all()

print(f"\nNodes: {len(nodes)}")
print(f"Rules: {len(rules)}")
print(f"Violations found: {len(reports)}")
for r in reports:
    print(f"  [{r.kind}] {r.description}")

# %%
# Correlation analysis
print("\n--- Correlation Analysis ---")
print("See src/polyalpha/correlation.py for:")
print("  - RollingCorrelationTracker")
print("  - CorrelationMatrix")
print("  - ClusterRegistry")
print("\nTo run correlation analysis on real data:")
print("  1. Collect order-book time series via 'polyalpha collect'")
print("  2. Build correlation matrix from price returns")
print("  3. Identify clusters via Union-Find")
print("  4. Enforce cluster exposure limits in risk engine")
