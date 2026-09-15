"""Effective sample size and bootstrap confidence intervals.

Section 21: Independence analysis — clustered forecasts are not
independent. Effective sample size accounts for within-cluster
correlation.

Section 22: Bootstrap CI — percentile-interval bootstrap with
cluster-level resampling.
"""

import random
from dataclasses import dataclass

from .research_dataset import ResearchDataset


@dataclass(frozen=True)
class EffectiveSampleSize:
    """Effective sample size analysis."""

    nominal_count: int
    effective_count: float
    design_effect: float  # nominal / effective
    cluster_count: int
    avg_cluster_size: float
    intra_cluster_correlation: float  # ICC
    unique_market_count: int
    unique_event_count: int

    def summary(self) -> dict:
        return {
            "nominal_count": self.nominal_count,
            "effective_count": round(self.effective_count, 1),
            "design_effect": round(self.design_effect, 4),
            "cluster_count": self.cluster_count,
            "avg_cluster_size": round(self.avg_cluster_size, 1),
            "intra_cluster_correlation": round(self.intra_cluster_correlation, 4),
            "unique_market_count": self.unique_market_count,
            "unique_event_count": self.unique_event_count,
        }


@dataclass(frozen=True)
class BootstrapCI:
    """Bootstrap confidence interval."""

    metric_name: str
    point_estimate: float
    ci_lower: float
    ci_upper: float
    ci_level: float  # e.g. 0.95
    bootstrap_samples: int
    std_error: float

    def summary(self) -> dict:
        return {
            "metric": self.metric_name,
            "point_estimate": round(self.point_estimate, 6),
            "ci_lower": round(self.ci_lower, 6),
            "ci_upper": round(self.ci_upper, 6),
            "ci_level": self.ci_level,
            "std_error": round(self.std_error, 6),
        }


def compute_effective_sample_size(dataset: ResearchDataset) -> EffectiveSampleSize:
    """Compute effective sample size accounting for cluster correlation."""
    records = [s for s in dataset.snapshots if s.final_resolution is not None]
    if not records:
        return EffectiveSampleSize(
            nominal_count=0,
            effective_count=0.0,
            design_effect=1.0,
            cluster_count=0,
            avg_cluster_size=0.0,
            intra_cluster_correlation=0.0,
            unique_market_count=0,
            unique_event_count=0,
        )

    unique_market_count = len({s.market_id for s in dataset.snapshots})
    unique_event_count = len({s.event_id for s in dataset.snapshots if s.event_id})

    # Group by cluster
    clusters: dict[str, list] = {}
    for snap in records:
        clusters.setdefault(snap.event_cluster, []).append(float(snap.model_probability))

    n = len(records)
    k = len(clusters)
    avg_size = n / k if k else 1.0

    # Compute ICC using ANOVA-style decomposition
    cluster_means = {c: sum(v) / len(v) for c, v in clusters.items()}
    grand_mean = sum(float(s.model_probability) for s in records) / n

    between_var = (
        sum(
            len(v) * (m - grand_mean) ** 2
            for v, m in zip(clusters.values(), cluster_means.values())
        )
        / (k - 1)
        if k > 1
        else 0.0
    )

    within_var = (
        sum(sum((x - cluster_means[c]) ** 2 for x in v) for c, v in clusters.items()) / (n - k)
        if n > k
        else 0.0
    )

    total_var = between_var + within_var
    icc = between_var / total_var if total_var > 0 else 0.0

    # Design effect = 1 + (avg_cluster_size - 1) * ICC
    design_effect = 1 + (avg_size - 1) * icc
    effective_n = n / design_effect if design_effect > 0 else n

    return EffectiveSampleSize(
        nominal_count=n,
        effective_count=effective_n,
        design_effect=design_effect,
        cluster_count=k,
        avg_cluster_size=avg_size,
        intra_cluster_correlation=icc,
        unique_market_count=unique_market_count,
        unique_event_count=unique_event_count,
    )


def bootstrap_confidence_interval(
    dataset: ResearchDataset,
    metric_fn=None,
    n_bootstrap: int = 2000,
    ci_level: float = 0.95,
    seed: int = 42,
) -> BootstrapCI:
    """Bootstrap confidence interval with cluster-level resampling.

    Resamples entire clusters (not individual records) to preserve
    within-cluster correlation structure.
    """
    records = [s for s in dataset.snapshots if s.final_resolution is not None]
    if not records:
        return BootstrapCI(
            metric_name="brier_score",
            point_estimate=1.0,
            ci_lower=1.0,
            ci_upper=1.0,
            ci_level=ci_level,
            bootstrap_samples=0,
            std_error=0.0,
        )

    if metric_fn is None:

        def metric_fn(recs: list) -> float:
            if not recs:
                return 1.0
            return sum((float(s.model_probability) - s.final_resolution) ** 2 for s in recs) / len(
                recs
            )

    # Group by cluster
    clusters: dict[str, list] = {}
    for snap in records:
        clusters.setdefault(snap.event_cluster, []).append(snap)

    cluster_keys = list(clusters.keys())
    rng = random.Random(seed)

    point_estimate = metric_fn(records)

    bootstrap_values = []
    for _ in range(n_bootstrap):
        # Resample clusters with replacement
        sampled_keys = rng.choices(cluster_keys, k=len(cluster_keys))
        sampled_records = []
        for key in sampled_keys:
            sampled_records.extend(clusters[key])
        bootstrap_values.append(metric_fn(sampled_records))

    bootstrap_values.sort()
    alpha = 1.0 - ci_level
    lower_idx = int(alpha / 2 * n_bootstrap)
    upper_idx = int((1 - alpha / 2) * n_bootstrap) - 1
    lower_idx = max(0, min(lower_idx, n_bootstrap - 1))
    upper_idx = max(0, min(upper_idx, n_bootstrap - 1))

    std_error = (sum((v - point_estimate) ** 2 for v in bootstrap_values) / n_bootstrap) ** 0.5

    return BootstrapCI(
        metric_name="brier_score",
        point_estimate=point_estimate,
        ci_lower=bootstrap_values[lower_idx],
        ci_upper=bootstrap_values[upper_idx],
        ci_level=ci_level,
        bootstrap_samples=n_bootstrap,
        std_error=std_error,
    )
