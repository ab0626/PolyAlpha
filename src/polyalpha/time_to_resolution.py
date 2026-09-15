"""Time-to-resolution analysis — performance by temporal distance.

Section 11 of the v0.3 spec: does model accuracy vary by how far out
the prediction is made relative to resolution?
"""

from dataclasses import dataclass

from .research_dataset import ResearchDataset


@dataclass(frozen=True)
class TimeBucket:
    """Result for a single time-to-resolution bucket."""

    bucket_label: str
    range_hours: tuple[float, float]
    brier_score: float
    market_brier: float
    delta_brier: float
    directional_accuracy: float
    mean_model_prob: float
    mean_market_mid: float
    mean_outcome: float
    count: int


@dataclass(frozen=True)
class TimeToResolutionAnalysis:
    """Full time-to-resolution analysis."""

    buckets: list[TimeBucket]
    decay_rate: float  # slope of brier vs time (positive = worse further out)
    best_horizon: str
    worst_horizon: str

    def summary(self) -> dict:
        return {
            "decay_rate": self.decay_rate,
            "best_horizon": self.best_horizon,
            "worst_horizon": self.worst_horizon,
            "bucket_count": len(self.buckets),
        }


def analyze_time_to_resolution(
    dataset: ResearchDataset,
) -> TimeToResolutionAnalysis:
    """Analyze model performance by time-to-resolution."""
    records = [
        s
        for s in dataset.snapshots
        if s.final_resolution is not None and s.resolution_timestamp is not None
    ]
    if not records:
        return TimeToResolutionAnalysis(
            buckets=[], decay_rate=0.0, best_horizon="", worst_horizon=""
        )

    # 10 fixed horizon buckets (hours)
    _FIXED_BUCKETS = [
        (">90d", 2160, float("inf")),
        ("60-90d", 1440, 2160),
        ("30-60d", 720, 1440),
        ("14-30d", 336, 720),
        ("7-14d", 168, 336),
        ("3-7d", 72, 168),
        ("1-3d", 24, 72),
        ("6-24h", 6, 24),
        ("1-6h", 1, 6),
        ("<1h", 0, 1),
    ]

    # Compute time-to-resolution in hours and assign to fixed buckets
    bucket_items: dict[str, list] = {label: [] for label, _, _ in _FIXED_BUCKETS}
    for snap in records:
        delta = snap.resolution_timestamp - snap.observation_timestamp
        hours = delta.total_seconds() / 3600
        if hours < 0:
            continue
        for label, lo, hi in _FIXED_BUCKETS:
            if lo <= hours < hi:
                bucket_items[label].append((snap, hours))
                break

    buckets = []
    time_points = []
    brier_points = []

    for label, lo, hi in _FIXED_BUCKETS:
        items = bucket_items[label]
        if not items:
            continue

        recs = [item[0] for item in items]
        model_probs = [float(s.model_probability) for s in recs]
        market_probs = [float(s.yes_mid) for s in recs] if recs[0].yes_mid else model_probs
        outcomes = [s.final_resolution for s in recs]

        brier = sum((p - o) ** 2 for p, o in zip(model_probs, outcomes)) / len(model_probs)
        mkt_brier = sum((p - o) ** 2 for p, o in zip(market_probs, outcomes)) / len(market_probs)

        correct = sum(1 for p, o in zip(model_probs, outcomes) if (p >= 0.5) == (o == 1))
        direction = correct / len(model_probs) if model_probs else 0.0

        if hi == float("inf"):
            bucket_label = f">{lo:.0f}h"
        else:
            bucket_label = f"{lo:.0f}h-{hi:.0f}h"
        buckets.append(
            TimeBucket(
                bucket_label=bucket_label,
                range_hours=(lo, hi if hi != float("inf") else 999999),
                brier_score=round(brier, 6),
                market_brier=round(mkt_brier, 6),
                delta_brier=round(mkt_brier - brier, 6),
                directional_accuracy=round(direction, 4),
                mean_model_prob=round(sum(model_probs) / len(model_probs), 4),
                mean_market_mid=round(sum(market_probs) / len(market_probs), 4)
                if market_probs
                else 0.0,
                mean_outcome=round(sum(outcomes) / len(outcomes), 4),
                count=len(recs),
            )
        )
        time_points.append((lo + (hi if hi != float("inf") else lo + 500)) / 2)
        brier_points.append(brier)

    # Simple linear regression for decay rate
    if len(time_points) >= 2:
        n = len(time_points)
        mx = sum(time_points) / n
        my = sum(brier_points) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(time_points, brier_points)) / (n - 1)
        var = sum((x - mx) ** 2 for x in time_points) / (n - 1)
        decay_rate = cov / var if var > 0 else 0.0
    else:
        decay_rate = 0.0

    best = min(buckets, key=lambda b: b.brier_score)
    worst = max(buckets, key=lambda b: b.brier_score)

    return TimeToResolutionAnalysis(
        buckets=buckets,
        decay_rate=round(decay_rate, 6),
        best_horizon=best.bucket_label,
        worst_horizon=worst.bucket_label,
    )
