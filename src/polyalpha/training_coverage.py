"""Training data coverage reporting.

Part 79: Reports how well a new prediction is covered by the training dataset
by category, time range, and similar market features.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CoverageReport:
    """Training data coverage assessment for a prediction."""

    category: str
    similar_market_count: int
    category_observation_count: int
    total_training_observations: int
    category_fraction: float
    time_range_coverage: float  # fraction of training period with observations
    is_low_coverage: bool
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "category": self.category,
            "similar_market_count": self.similar_market_count,
            "category_observation_count": self.category_observation_count,
            "total_training_observations": self.total_training_observations,
            "category_fraction": self.category_fraction,
            "time_range_coverage": self.time_range_coverage,
            "is_low_coverage": self.is_low_coverage,
            "warnings": self.warnings,
        }


def report_coverage(
    prediction: dict,
    training_dataset: list[dict],
    category: str,
    time_range: tuple[str, str] | None = None,
) -> CoverageReport:
    """Report training data coverage for a new prediction.

    Args:
        prediction: Dict with keys like 'category', 'features', 'timestamp'.
        training_dataset: List of training observation dicts.
        category: Category label for the prediction.
        time_range: Optional (start, end) ISO timestamps for the training period.

    Returns:
        CoverageReport with counts and flags for low coverage.
    """
    warnings: list[str] = []

    # Filter training data by category
    cat_observations = [t for t in training_dataset if t.get("category") == category]
    total = len(training_dataset)
    cat_count = len(cat_observations)
    cat_fraction = cat_count / total if total > 0 else 0.0

    # Count similar markets by feature overlap
    pred_features = prediction.get("features", {})
    similar_count = 0
    for obs in cat_observations:
        obs_features = obs.get("features", {})
        overlap = sum(
            1 for k in pred_features
            if k in obs_features and abs(float(pred_features[k]) - float(obs_features[k])) < 0.1
        )
        if overlap >= max(1, len(pred_features) // 2):
            similar_count += 1

    # Time range coverage
    time_coverage = 1.0
    if time_range and len(time_range) == 2:
        start_str, end_str = time_range
        timestamps = [
            t.get("timestamp", "")
            for t in training_dataset
            if t.get("timestamp")
        ]
        if timestamps:
            in_range = sum(1 for ts in timestamps if start_str <= ts <= end_str)
            time_coverage = in_range / total if total > 0 else 0.0

    # Low coverage checks
    is_low = False
    if cat_count < 10:
        warnings.append(f"LOW CATEGORY COVERAGE: only {cat_count} observations for '{category}'")
        is_low = True
    if cat_fraction < 0.05:
        warnings.append(
            f"IMBALANCED CATEGORY: '{category}' is {cat_fraction:.1%} of training data"
        )
        is_low = True
    if similar_count < 5:
        warnings.append(
            f"FEW SIMILAR MARKETS: only {similar_count} similar training observations found"
        )
        is_low = True
    if time_coverage < 0.5:
        warnings.append(
            f"PARTIAL TIME COVERAGE: only {time_coverage:.1%} of training period has data"
        )
        is_low = True

    return CoverageReport(
        category=category,
        similar_market_count=similar_count,
        category_observation_count=cat_count,
        total_training_observations=total,
        category_fraction=round(cat_fraction, 4),
        time_range_coverage=round(time_coverage, 4),
        is_low_coverage=is_low,
        warnings=warnings,
    )
