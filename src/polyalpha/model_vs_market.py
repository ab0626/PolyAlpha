"""Model vs Market comparison matrix.

Section 9 of the v0.3 spec: bucketed comparison showing where model
beats market and where it doesn't.
"""

from dataclasses import dataclass

from .research_dataset import ResearchDataset


@dataclass(frozen=True)
class BucketComparison:
    """Comparison for a single probability bucket."""

    bucket_label: str
    model_brier: float
    market_brier: float
    delta_brier: float
    model_wins: int
    market_wins: int
    ties: int
    count: int
    mean_outcome: float


@dataclass(frozen=True)
class MarketModelMatrix:
    """Full model vs market comparison matrix."""

    bucket_comparisons: list[BucketComparison]
    overall_delta_brier: float
    overall_model_wins: int
    overall_market_wins: int
    total_compared: int

    def summary(self) -> dict:
        return {
            "overall_delta_brier": self.overall_delta_brier,
            "model_wins_pct": round(self.overall_model_wins / self.total_compared * 100, 1)
            if self.total_compared
            else 0,
            "market_wins_pct": round(self.overall_market_wins / self.total_compared * 100, 1)
            if self.total_compared
            else 0,
            "total_compared": self.total_compared,
            "buckets": len(self.bucket_comparisons),
        }


def build_market_model_matrix(
    dataset: ResearchDataset,
    n_buckets: int = 5,
) -> MarketModelMatrix:
    """Build bucketed model vs market comparison matrix."""
    records = [
        s for s in dataset.snapshots if s.final_resolution is not None and s.yes_mid is not None
    ]
    if not records:
        return MarketModelMatrix(
            bucket_comparisons=[],
            overall_delta_brier=0.0,
            overall_model_wins=0,
            overall_market_wins=0,
            total_compared=0,
        )

    # Bucket by model probability
    buckets: dict[int, list] = {}
    for snap in records:
        p = float(snap.model_probability)
        b = min(int(p * n_buckets), n_buckets - 1)
        buckets.setdefault(b, []).append(snap)

    comparisons = []
    total_model_wins = total_market_wins = total_ties = 0
    all_model_brier = all_market_brier = 0.0
    total_count = 0

    for b in range(n_buckets):
        recs = buckets.get(b, [])
        if not recs:
            continue

        model_probs = [float(s.model_probability) for s in recs]
        market_probs = [float(s.yes_mid) for s in recs]
        outcomes = [s.final_resolution for s in recs]

        model_b = sum((p - o) ** 2 for p, o in zip(model_probs, outcomes)) / len(model_probs)
        market_b = sum((p - o) ** 2 for p, o in zip(market_probs, outcomes)) / len(market_probs)

        m_wins = mkt_wins = tie = 0
        for mp, mdp, o in zip(market_probs, model_probs, outcomes):
            me = abs(mp - o)
            md = abs(mdp - o)
            if me < md:
                mkt_wins += 1
            elif md < me:
                m_wins += 1
            else:
                tie += 1

        comparisons.append(
            BucketComparison(
                bucket_label=f"{b / n_buckets:.1f}-{(b + 1) / n_buckets:.1f}",
                model_brier=round(model_b, 6),
                market_brier=round(market_b, 6),
                delta_brier=round(market_b - model_b, 6),
                model_wins=m_wins,
                market_wins=mkt_wins,
                ties=tie,
                count=len(recs),
                mean_outcome=round(sum(outcomes) / len(outcomes), 4),
            )
        )
        total_model_wins += m_wins
        total_market_wins += mkt_wins
        total_ties += tie
        all_model_brier += model_b * len(recs)
        all_market_brier += market_b * len(recs)
        total_count += len(recs)

    overall_delta = (all_market_brier - all_model_brier) / total_count if total_count else 0.0

    return MarketModelMatrix(
        bucket_comparisons=comparisons,
        overall_delta_brier=round(overall_delta, 6),
        overall_model_wins=total_model_wins,
        overall_market_wins=total_market_wins,
        total_compared=total_count,
    )
