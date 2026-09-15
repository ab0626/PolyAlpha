"""Feature importance analysis using permutation importance.

Detects which features contribute most to model predictions and
identifies potential data leakage (features that suspiciously
encode future information).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

D = Decimal


class ScorableModel(Protocol):
    """Any model that can produce a probability score from features."""

    def score(self, features: dict[str, float]) -> float: ...


@dataclass(frozen=True)
class FeatureImportance:
    """Permutation importance result for a single feature."""

    feature_name: str
    importance: float
    baseline_score: float
    permuted_score: float
    direction: str  # "positive" or "negative"


def permutation_importance(
    model: ScorableModel,
    feature_names: list[str],
    X: list[dict[str, float]],
    y: list[int],
    n_repeats: int = 5,
    random_state: int = 42,
) -> list[FeatureImportance]:
    """Compute permutation importance for each feature.

    For each feature:
    1. Measure baseline model performance
    2. Permute the feature column
    3. Measure performance drop
    4. Importance = baseline - permuted

    Large positive importance = feature is useful
    Large negative importance = feature may encode leakage

    Args:
        model: Model with a score(features_dict) -> float method
        feature_names: Names of features to evaluate
        X: List of feature dictionaries
        y: List of binary outcomes (0 or 1)
        n_repeats: Number of permutation repeats
        random_state: Random seed for reproducibility

    Returns:
        List of FeatureImportance sorted by absolute importance
    """
    import random

    if not X or not y or len(X) != len(y):
        raise ValueError("nonempty aligned observations required")

    rng = random.Random(random_state)

    def _score(data: list[dict], outcomes: list[int]) -> float:
        predictions = []
        for row in data:
            try:
                p = model.score(row)
                predictions.append(max(1e-12, min(1 - 1e-12, p)))
            except Exception:
                predictions.append(0.5)
        return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(outcomes)

    # Baseline score (lower is better for Brier score)
    baseline = _score(X, y)

    results = []
    for feature in feature_names:
        scores = []
        for _ in range(n_repeats):
            # Create permuted dataset
            X_permuted = [dict(row) for row in X]
            values = [row.get(feature, 0.0) for row in X_permuted]
            permuted_values = values[:]
            rng.shuffle(permuted_values)
            for row, pv in zip(X_permuted, permuted_values):
                row[feature] = pv

            permuted_score = _score(X_permuted, y)
            scores.append(permuted_score)

        mean_permuted = sum(scores) / len(scores)
        importance = mean_permuted - baseline  # positive = feature helps

        results.append(
            FeatureImportance(
                feature_name=feature,
                importance=importance,
                baseline_score=baseline,
                permuted_score=mean_permuted,
                direction="positive" if importance > 0 else "negative",
            )
        )

    return sorted(results, key=lambda r: abs(r.importance), reverse=True)


def detect_leakage(
    importances: list[FeatureImportance],
    threshold: float = 0.05,
) -> list[FeatureImportance]:
    """Flag features with suspiciously high negative importance.

    A feature with large negative importance means the model performs
    BETTER when that feature is permuted. This could indicate:
    - The feature encodes future information (leakage)
    - The feature is adversarial/noisy
    - The feature has a counterintuitive relationship

    Features with large positive importance are useful but should
    also be checked for leakage if they seem too good.
    """
    suspicious = [imp for imp in importances if imp.importance < -threshold]
    return sorted(suspicious, key=lambda r: r.importance)


def feature_summary(importances: list[FeatureImportance]) -> dict:
    """Human-readable summary of feature importance results."""
    if not importances:
        return {"total_features": 0, "leakage_suspects": []}

    suspects = detect_leakage(importances)
    top_positive = [i for i in importances if i.importance > 0][:5]
    top_negative = [i for i in importances if i.importance < 0][:5]

    return {
        "total_features": len(importances),
        "baseline_brier": importances[0].baseline_score if importances else None,
        "top_positive_features": [
            {"feature": i.feature_name, "importance": i.importance} for i in top_positive
        ],
        "top_negative_features": [
            {"feature": i.feature_name, "importance": i.importance} for i in top_negative
        ],
        "leakage_suspects": [
            {"feature": i.feature_name, "importance": i.importance} for i in suspects
        ],
        "recommendation": (
            "Investigate negative-importance features for potential data leakage."
            if suspects
            else "No obvious leakage detected."
        ),
    }
