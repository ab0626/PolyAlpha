"""Edge realization metrics.

Tracks predicted vs realized outcomes for evaluating model quality
and edge decay over time.
"""

from dataclasses import dataclass
from decimal import Decimal
from statistics import mean

D = Decimal


@dataclass(frozen=True)
class EdgeObservation:
    """Single observation of predicted vs realized edge."""

    market_id: str
    predicted_at: str
    fair_probability: Decimal
    execution_price: Decimal
    side: str
    predicted_edge: Decimal
    realized_outcome: int | None = None
    realized_pnl: Decimal | None = None


def edge_realization_ratio(observations: list[EdgeObservation]) -> dict:
    """Compute edge realization ratio.

    ratio = (trades where realized_edge > 0) / (trades where predicted_edge > 0)

    > 0.5 means more than half of positive-edge predictions were profitable.
    """
    filled = [o for o in observations if o.realized_outcome is not None]
    positive_pred = [o for o in filled if o.predicted_edge > 0]

    if not positive_pred:
        return {
            "ratio": None,
            "sample_size": 0,
            "total_predicted_positive": 0,
            "total_realized_positive": 0,
        }

    realized_positive = [
        o for o in positive_pred if o.realized_pnl is not None and o.realized_pnl > 0
    ]

    return {
        "ratio": len(realized_positive) / len(positive_pred),
        "sample_size": len(positive_pred),
        "total_predicted_positive": len(positive_pred),
        "total_realized_positive": len(realized_positive),
    }


def realized_edge_stats(observations: list[EdgeObservation]) -> dict:
    """Compute statistics on realized edges."""
    filled = [o for o in observations if o.realized_pnl is not None]

    if not filled:
        return {
            "mean_realized_pnl": None,
            "median_realized_pnl": None,
            "win_rate": None,
            "avg_winning_edge": None,
            "avg_losing_edge": None,
            "edge_decay": None,
            "sample_size": 0,
        }

    pnls = [float(o.realized_pnl) for o in filled]
    wins = [o for o in filled if o.realized_pnl > 0]
    losses = [o for o in filled if o.realized_pnl <= 0]

    # Edge decay: average predicted edge for winning trades vs losing trades
    avg_win_edge = mean([float(o.predicted_edge) for o in wins]) if wins else None
    avg_loss_edge = mean([float(o.predicted_edge) for o in losses]) if losses else None

    return {
        "mean_realized_pnl": mean(pnls),
        "median_realized_pnl": sorted(pnls)[len(pnls) // 2],
        "win_rate": len(wins) / len(filled) if filled else 0,
        "avg_winning_edge": avg_win_edge,
        "avg_losing_edge": avg_loss_edge,
        "edge_decay": avg_win_edge - avg_loss_edge if avg_win_edge and avg_loss_edge else None,
        "sample_size": len(filled),
    }


def edge_by_confidence_bucket(
    observations: list[EdgeObservation],
    bucket_size: Decimal = D("0.05"),
) -> dict:
    """Bucket edge realization by predicted edge magnitude."""
    buckets: dict[str, list[EdgeObservation]] = {}

    for obs in observations:
        if obs.realized_outcome is None:
            continue
        bucket_idx = int(obs.predicted_edge / bucket_size)
        bucket_key = f"{bucket_idx * bucket_size:.2f}-{(bucket_idx + 1) * bucket_size:.2f}"
        buckets.setdefault(bucket_key, []).append(obs)

    result = {}
    for bucket_key, obs_list in sorted(buckets.items()):
        wins = [o for o in obs_list if o.realized_pnl and o.realized_pnl > 0]
        result[bucket_key] = {
            "count": len(obs_list),
            "win_rate": len(wins) / len(obs_list) if obs_list else 0,
            "avg_pnl": mean([float(o.realized_pnl) for o in obs_list if o.realized_pnl])
            if obs_list
            else None,
        }

    return result
