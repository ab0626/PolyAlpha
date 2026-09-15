"""Statistical significance testing and multiple hypothesis correction.

Provides clustered bootstrap confidence intervals, multiple testing
corrections (Bonferroni, Benjamini-Hochberg), effect size computation,
and warnings for multiple hypothesis testing.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class BootstrapCI:
    """Bootstrap confidence interval result."""

    estimate: float
    ci_5: float
    ci_50: float
    ci_95: float
    std_error: float
    n_bootstrap: int


@dataclass(frozen=True)
class CorrectionResult:
    """Multiple testing correction result."""

    method: str
    original_p_values: list[float]
    adjusted_p_values: list[float]
    rejected_at_005: list[bool]
    rejected_at_010: list[bool]


@dataclass(frozen=True)
class EffectSizeResult:
    """Effect size computation result."""

    cohens_d: float
    interpretation: str
    brier_model: float
    brier_market: float
    pooled_std: float


@dataclass(frozen=True)
class StatisticalResult:
    """Comprehensive statistical analysis result."""

    bootstrap: BootstrapCI | None = None
    bonferroni: CorrectionResult | None = None
    benjamini_hochberg: CorrectionResult | None = None
    effect_size: EffectSizeResult | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        result = {"warnings": self.warnings}
        if self.bootstrap:
            result["bootstrap"] = {
                "estimate": self.bootstrap.estimate,
                "ci_5": self.bootstrap.ci_5,
                "ci_95": self.bootstrap.ci_95,
            }
        if self.bonferroni:
            result["bonferroni"] = {
                "n_rejected_005": sum(self.bonferroni.rejected_at_005),
            }
        if self.benjamini_hochberg:
            result["benjamini_hochberg"] = {
                "n_rejected_005": sum(self.benjamini_hochberg.rejected_at_005),
            }
        if self.effect_size:
            result["effect_size"] = {
                "cohens_d": self.effect_size.cohens_d,
                "interpretation": self.effect_size.interpretation,
            }
        return result


def clustered_bootstrap_ci(
    data: list[float],
    stat_fn,
    cluster_ids: list[int | str],
    n_bootstrap: int = 2000,
    seed: int | None = None,
) -> BootstrapCI:
    """Compute clustered bootstrap confidence intervals.

    Resamples entire clusters (not individual observations) to account
    for within-cluster correlation.

    Args:
        data: Observed values.
        stat_fn: Function computing the statistic of interest.
        cluster_ids: Cluster assignment for each observation.
        n_bootstrap: Number of bootstrap resamples.
        seed: Random seed for reproducibility.

    Returns:
        BootstrapCI with point estimate and percentile CIs.
    """
    rng = random.Random(seed)
    n = len(data)
    if n == 0:
        return BootstrapCI(0.0, 0.0, 0.0, 0.0, 0.0, n_bootstrap)

    # Group by cluster
    clusters: dict[int | str, list[float]] = defaultdict(list)
    for val, cid in zip(data, cluster_ids):
        clusters[cid].append(val)

    cluster_keys = list(clusters.keys())
    point_estimate = stat_fn(data)

    bootstrap_stats: list[float] = []
    for _ in range(n_bootstrap):
        # Resample clusters with replacement
        sampled_keys = rng.choices(cluster_keys, k=len(cluster_keys))
        resampled: list[float] = []
        for key in sampled_keys:
            resampled.extend(clusters[key])
        bootstrap_stats.append(stat_fn(resampled))

    bootstrap_stats.sort()
    ci_5 = bootstrap_stats[int(0.05 * len(bootstrap_stats))]
    ci_50 = bootstrap_stats[int(0.50 * len(bootstrap_stats))]
    ci_95 = bootstrap_stats[int(0.95 * len(bootstrap_stats))]

    mean_boot = sum(bootstrap_stats) / len(bootstrap_stats)
    variance = sum((s - mean_boot) ** 2 for s in bootstrap_stats) / len(bootstrap_stats)
    std_error = math.sqrt(variance)

    return BootstrapCI(
        estimate=round(point_estimate, 6),
        ci_5=round(ci_5, 6),
        ci_50=round(ci_50, 6),
        ci_95=round(ci_95, 6),
        std_error=round(std_error, 6),
        n_bootstrap=n_bootstrap,
    )


def bonferroni_correction(p_values: list[float]) -> CorrectionResult:
    """Apply Bonferroni correction for multiple testing.

    Adjusted p-value = min(original * n_tests, 1.0).
    """
    n = len(p_values)
    adjusted = [min(p * n, 1.0) for p in p_values]

    return CorrectionResult(
        method="bonferroni",
        original_p_values=p_values,
        adjusted_p_values=adjusted,
        rejected_at_005=[a < 0.05 for a in adjusted],
        rejected_at_010=[a < 0.10 for a in adjusted],
    )


def benjamini_hochberg(p_values: list[float]) -> CorrectionResult:
    """Apply Benjamini-Hochberg FDR correction.

    Controls false discovery rate at the given level.
    """
    n = len(p_values)
    if n == 0:
        return CorrectionResult(
            method="benjamini_hochberg",
            original_p_values=[],
            adjusted_p_values=[],
            rejected_at_005=[],
            rejected_at_010=[],
        )

    indexed = sorted(enumerate(p_values), key=lambda x: x[1], reverse=True)
    adjusted = [0.0] * n
    running_max = 0.0

    for rank, (orig_idx, p) in enumerate(indexed, 1):
        adjusted_val = min(p * n / rank, 1.0)
        running_max = max(running_max, adjusted_val)
        adjusted[orig_idx] = running_max

    return CorrectionResult(
        method="benjamini_hochberg",
        original_p_values=p_values,
        adjusted_p_values=adjusted,
        rejected_at_005=[a < 0.05 for a in adjusted],
        rejected_at_010=[a < 0.10 for a in adjusted],
    )


def multiple_testing_warning(
    n_tested: int,
    best_p_value: float,
) -> str:
    """Generate warning message for multiple hypothesis testing.

    Estimates the expected number of false positives under the null
    and compares with the best observed p-value.
    """
    if n_tested <= 0:
        return "No tests conducted."

    expected_false_positives_005 = n_tested * 0.05

    if best_p_value < 0.05 / n_tested:
        return (
            f"Bonferroni-significant result found (best p={best_p_value:.4f}, "
            f"threshold={0.05 / n_tested:.4f}). {n_tested} tests conducted. "
            f"Expected {expected_false_positives_005:.1f} false positives at alpha=0.05."
        )
    if best_p_value < 0.05:
        return (
            f"Best p-value ({best_p_value:.4f}) is nominally significant but "
            f"does not survive Bonferroni correction (threshold={0.05 / n_tested:.4f}). "
            f"{n_tested} tests conducted. "
            f"Expected {expected_false_positives_005:.1f} false positives at alpha=0.05. "
            f"Consider Benjamini-Hochberg for less conservative control."
        )
    return (
        f"No significant results after {n_tested} tests. "
        f"Best p-value: {best_p_value:.4f}."
    )


def compute_effect_size(
    brier_model: float,
    brier_market: float,
    std: float | None = None,
) -> EffectSizeResult:
    """Compute Cohen's d effect size for model vs market Brier scores.

    A positive d indicates the model outperforms the market.
    """
    diff = brier_market - brier_model

    if std is None or std <= 0:
        # Estimate pooled std from Brier score bounds [0, 1]
        std = math.sqrt(0.25)

    cohens_d = diff / std if std > 0 else 0.0

    if abs(cohens_d) >= 0.8:
        interpretation = "large"
    elif abs(cohens_d) >= 0.5:
        interpretation = "medium"
    elif abs(cohens_d) >= 0.2:
        interpretation = "small"
    else:
        interpretation = "negligible"

    return EffectSizeResult(
        cohens_d=round(cohens_d, 4),
        interpretation=interpretation,
        brier_model=brier_model,
        brier_market=brier_market,
        pooled_std=round(std, 4),
    )


def full_statistical_analysis(
    data: list[float],
    stat_fn,
    cluster_ids: list[int | str],
    p_values: list[float] | None = None,
    brier_model: float | None = None,
    brier_market: float | None = None,
    std: float | None = None,
    n_bootstrap: int = 2000,
) -> StatisticalResult:
    """Run complete statistical analysis pipeline.

    Computes bootstrap CIs, multiple testing corrections, effect sizes,
    and generates appropriate warnings.
    """
    bootstrap = clustered_bootstrap_ci(data, stat_fn, cluster_ids, n_bootstrap)

    bonf = None
    bh = None
    warnings_list: list[str] = []
    best_p = 1.0

    if p_values:
        bonf = bonferroni_correction(p_values)
        bh = benjamini_hochberg(p_values)
        best_p = min(p_values) if p_values else 1.0
        warnings_list.append(multiple_testing_warning(len(p_values), best_p))

    effect = None
    if brier_model is not None and brier_market is not None:
        effect = compute_effect_size(brier_model, brier_market, std)
        if effect.interpretation == "negligible":
            warnings_list.append(
                f"Effect size is negligible (d={effect.cohens_d}). "
                "Results may not be practically significant."
            )

    return StatisticalResult(
        bootstrap=bootstrap,
        bonferroni=bonf,
        benjamini_hochberg=bh,
        effect_size=effect,
        warnings=warnings_list,
    )
