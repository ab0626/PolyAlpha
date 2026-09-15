"""Model staleness detection — monitor model degradation over time.

Detects when a trained model has become stale by comparing prediction
distributions, calibration, signal counts, and edge distributions
between model versions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class DistributionShift:
    """Metrics for prediction distribution shift between versions."""

    mean_diff: float
    std_diff: float
    ks_statistic: float  # Kolmogorov-Smirnov-like statistic
    max_abs_diff: float
    calibration_shift: float
    signal_count_change: int
    signal_count_change_pct: float
    edge_mean_diff: float
    edge_std_diff: float
    category_distribution_shift: float

    def summary(self) -> dict:
        return {
            "mean_diff": self.mean_diff,
            "ks_statistic": self.ks_statistic,
            "calibration_shift": self.calibration_shift,
            "signal_count_change_pct": self.signal_count_change_pct,
            "edge_mean_diff": self.edge_mean_diff,
        }


@dataclass(frozen=True)
class StalenessReport:
    """Model staleness analysis result."""

    is_stale: bool
    age_days: float
    max_age_days: int
    staleness_score: float  # 0.0 = fresh, 1.0 = very stale
    distribution_shift: DistributionShift | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "is_stale": self.is_stale,
            "age_days": self.age_days,
            "staleness_score": self.staleness_score,
            "warnings": self.warnings,
        }


def _ks_statistic(v1: list[float], v2: list[float]) -> float:
    """Compute maximum difference between two empirical CDFs."""
    if not v1 or not v2:
        return 0.0
    all_vals = sorted(set(v1 + v2))
    max_diff = 0.0

    for val in all_vals:
        cdf1 = sum(1 for x in v1 if x <= val) / len(v1)
        cdf2 = sum(1 for x in v2 if x <= val) / len(v2)
        max_diff = max(max_diff, abs(cdf1 - cdf2))

    return round(max_diff, 6)


def _js_divergence(p: list[float], q: list[float], n_bins: int = 10) -> float:
    """Compute Jensen-Shannon divergence between two distributions."""
    if not p or not q:
        return 0.0

    min_p, max_p = min(p), max(p)
    min_q, max_q = min(q), max(q)
    lo = min(min_p, min_q)
    hi = max(max_p, max_q)
    if hi <= lo:
        return 0.0
    bin_width = (hi - lo) / n_bins

    hist_p = [0.0] * n_bins
    hist_q = [0.0] * n_bins

    for val in p:
        idx = min(int((val - lo) / bin_width), n_bins - 1)
        hist_p[idx] += 1
    for val in q:
        idx = min(int((val - lo) / bin_width), n_bins - 1)
        hist_q[idx] += 1

    # Normalize
    sum_p = sum(hist_p) or 1.0
    sum_q = sum(hist_q) or 1.0
    hist_p = [x / sum_p for x in hist_p]
    hist_q = [x / sum_q for x in hist_q]

    # Jensen-Shannon
    m = [(a + b) / 2 for a, b in zip(hist_p, hist_q)]
    js = 0.0
    for pi, qi, mi in zip(hist_p, hist_q, m):
        if pi > 0 and mi > 0:
            js += 0.5 * pi * math.log(pi / mi)
        if qi > 0 and mi > 0:
            js += 0.5 * qi * math.log(qi / mi)

    return round(js, 6)


def detect_staleness(
    model_trained_at: datetime,
    current_time: datetime,
    max_age_days: int = 30,
) -> StalenessReport:
    """Check if a model is stale based on age alone.

    Args:
        model_trained_at: When the model was last trained.
        current_time: Current timestamp.
        max_age_days: Maximum acceptable model age.

    Returns:
        StalenessReport with age-based staleness assessment.
    """
    age_delta = current_time - model_trained_at
    age_days = age_delta.total_seconds() / 86400

    is_stale = age_days > max_age_days
    staleness_score = min(1.0, age_days / max_age_days) if max_age_days > 0 else 1.0

    warnings: list[str] = []
    if is_stale:
        warnings.append(
            f"Model is {age_days:.1f} days old (max allowed: {max_age_days}). "
            "Retraining recommended."
        )
    elif staleness_score > 0.7:
        warnings.append(
            f"Model is {age_days:.1f} days old ({staleness_score:.0%} of max age). "
            "Schedule retraining soon."
        )

    return StalenessReport(
        is_stale=is_stale,
        age_days=round(age_days, 2),
        max_age_days=max_age_days,
        staleness_score=round(staleness_score, 4),
        warnings=warnings,
    )


def compare_model_versions(
    predictions_v1: list[dict],
    predictions_v2: list[dict],
) -> DistributionShift:
    """Compare prediction distributions between two model versions.

    Checks for distribution shift in predictions, calibration,
    signal counts, edge distributions, and category distributions.

    Args:
        predictions_v1: List of prediction dicts from version 1 with keys:
            - forecast: float
            - actual: float (outcome)
            - edge: float
            - category: str
            - calibration_bucket: str
        predictions_v2: Same format for version 2.

    Returns:
        DistributionShift with all comparison metrics.
    """
    v1_forecasts = [p.get("forecast", 0.5) for p in predictions_v1]
    v2_forecasts = [p.get("forecast", 0.5) for p in predictions_v2]
    v1_edges = [p.get("edge", 0.0) for p in predictions_v1]
    v2_edges = [p.get("edge", 0.0) for p in predictions_v2]
    v1_actuals = [p.get("actual", 0) for p in predictions_v1]
    v2_actuals = [p.get("actual", 0) for p in predictions_v2]

    # Mean and std differences
    mean_v1 = sum(v1_forecasts) / len(v1_forecasts) if v1_forecasts else 0.5
    mean_v2 = sum(v2_forecasts) / len(v2_forecasts) if v2_forecasts else 0.5
    mean_diff = mean_v2 - mean_v1

    std_v1 = (
        math.sqrt(sum((x - mean_v1) ** 2 for x in v1_forecasts) / len(v1_forecasts))
        if len(v1_forecasts) > 1
        else 0.0
    )
    std_v2 = (
        math.sqrt(sum((x - mean_v2) ** 2 for x in v2_forecasts) / len(v2_forecasts))
        if len(v2_forecasts) > 1
        else 0.0
    )
    std_diff = std_v2 - std_v1

    # KS statistic
    ks = _ks_statistic(v1_forecasts, v2_forecasts)

    # Max absolute CDF difference
    max_abs = max(abs(mean_diff), ks)

    # Calibration shift: compare mean predicted vs mean actual
    cal_v1 = mean_v1 - (sum(v1_actuals) / len(v1_actuals) if v1_actuals else 0.5)
    cal_v2 = mean_v2 - (sum(v2_actuals) / len(v2_actuals) if v2_actuals else 0.5)
    calibration_shift = cal_v2 - cal_v1

    # Signal count change
    count_v1 = len(predictions_v1)
    count_v2 = len(predictions_v2)
    count_change = count_v2 - count_v1
    count_pct = (count_change / count_v1 * 100) if count_v1 > 0 else 0.0

    # Edge distribution shift
    edge_mean_v1 = sum(v1_edges) / len(v1_edges) if v1_edges else 0.0
    edge_mean_v2 = sum(v2_edges) / len(v2_edges) if v2_edges else 0.0
    edge_mean_diff = edge_mean_v2 - edge_mean_v1

    edge_std_v1 = (
        math.sqrt(sum((x - edge_mean_v1) ** 2 for x in v1_edges) / len(v1_edges))
        if len(v1_edges) > 1
        else 0.0
    )
    edge_std_v2 = (
        math.sqrt(sum((x - edge_mean_v2) ** 2 for x in v2_edges) / len(v2_edges))
        if len(v2_edges) > 1
        else 0.0
    )
    edge_std_diff = edge_std_v2 - edge_std_v1

    # Category distribution shift (JS divergence)
    cats_v1 = [p.get("category", "unknown") for p in predictions_v1]
    cats_v2 = [p.get("category", "unknown") for p in predictions_v2]
    all_cats = list(set(cats_v1 + cats_v2))
    cat_dist_v1 = [cats_v1.count(c) / len(cats_v1) if cats_v1 else 0 for c in all_cats]
    cat_dist_v2 = [cats_v2.count(c) / len(cats_v2) if cats_v2 else 0 for c in all_cats]
    cat_shift = _js_divergence(cat_dist_v1, cat_dist_v2, n_bins=len(all_cats) or 1)

    return DistributionShift(
        mean_diff=round(mean_diff, 6),
        std_diff=round(std_diff, 6),
        ks_statistic=ks,
        max_abs_diff=round(max_abs, 6),
        calibration_shift=round(calibration_shift, 6),
        signal_count_change=count_change,
        signal_count_change_pct=round(count_pct, 2),
        edge_mean_diff=round(edge_mean_diff, 6),
        edge_std_diff=round(edge_std_diff, 6),
        category_distribution_shift=round(cat_shift, 6),
    )


def full_staleness_check(
    model_trained_at: datetime,
    current_time: datetime,
    predictions_v1: list[dict] | None = None,
    predictions_v2: list[dict] | None = None,
    max_age_days: int = 30,
    shift_threshold: float = 0.1,
) -> StalenessReport:
    """Run complete staleness check combining age and distribution analysis."""
    age_report = detect_staleness(model_trained_at, current_time, max_age_days)

    shift = None
    warnings = list(age_report.warnings)

    if predictions_v1 is not None and predictions_v2 is not None:
        shift = compare_model_versions(predictions_v1, predictions_v2)

        if shift.ks_statistic > shift_threshold:
            warnings.append(
                f"Distribution shift detected (KS={shift.ks_statistic:.4f}, "
                f"threshold={shift_threshold}). Model predictions have changed significantly."
            )
        if abs(shift.calibration_shift) > 0.05:
            warnings.append(
                f"Calibration shifted by {shift.calibration_shift:.4f}. "
                "Recalibration may be needed."
            )
        if shift.signal_count_change_pct > 20:
            warnings.append(
                f"Signal count changed by {shift.signal_count_change_pct:.1f}%. "
                "Investigate feature drift."
            )

    stale = age_report.is_stale or shift is not None and shift.ks_statistic > shift_threshold
    score = max(
        age_report.staleness_score,
        (shift.ks_statistic if shift else 0.0),
    )

    return StalenessReport(
        is_stale=stale,
        age_days=age_report.age_days,
        max_age_days=age_report.max_age_days,
        staleness_score=min(1.0, round(score, 4)),
        distribution_shift=shift,
        warnings=warnings,
    )
