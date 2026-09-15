"""True out-of-domain testing for model generalization.

Part 28: Trains on specified categories and tests on excluded ones to
measure genuine out-of-distribution performance. Includes leave-one-out
ablation to identify which categories contribute most to generalization.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

D = Decimal

CategoryId: TypeAlias = str


@dataclass(frozen=True)
class CategoryMetrics:
    """Per-category evaluation metrics."""

    category: CategoryId
    brier_score: float
    log_loss: float
    ece: float
    sample_count: int
    outcome_mean: float
    predicted_mean: float


@dataclass(frozen=True)
class OODResult:
    """Result of an out-of-domain test."""

    train_categories: list[CategoryId]
    test_categories: list[CategoryId]
    train_metrics: CategoryMetrics
    test_metrics: CategoryMetrics
    brier_degradation: float  # test_brier - train_brier (positive = worse OOD)
    log_loss_degradation: float
    ece_degradation: float
    is_degraded: bool  # True if OOD performance significantly worse


@dataclass(frozen=True)
class AblationResult:
    """Result of leave-one-out category ablation."""

    excluded_category: CategoryId
    remaining_categories: list[CategoryId]
    ood_result: OODResult
    category_importance: float  # how much excluding this category hurts OOD


@dataclass(frozen=True)
class AblationReport:
    """Full ablation report across all categories."""

    categories: list[CategoryId]
    ablation_results: list[AblationResult]
    most_important_category: CategoryId
    least_important_category: CategoryId
    mean_ood_degradation: float
    summary: dict[str, float]  # category -> importance score


def _brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    if not probabilities:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes)) / len(probabilities)


def _log_loss(probabilities: list[float], outcomes: list[int]) -> float:
    if not probabilities:
        return 0.0
    eps = 1e-15
    total = 0.0
    for p, o in zip(probabilities, outcomes):
        p = max(eps, min(1.0 - eps, p))
        total -= math.log(p if o == 1 else 1.0 - p)
    return total / len(probabilities)


def _expected_calibration_error(
    probabilities: list[float], outcomes: list[int], n_buckets: int = 10
) -> float:
    if not probabilities:
        return 0.0
    buckets: dict[int, list[tuple[float, int]]] = {}
    for p, o in zip(probabilities, outcomes):
        buckets.setdefault(min(int(p * n_buckets), n_buckets - 1), []).append((p, o))
    total_error = total_count = 0
    for b in range(n_buckets):
        items = buckets.get(b, [])
        if items:
            mean_pred = sum(p for p, _ in items) / len(items)
            mean_actual = sum(o for _, o in items) / len(items)
            total_error += abs(mean_pred - mean_actual) * len(items)
            total_count += len(items)
    return total_error / total_count if total_count else 0.0


def _compute_category_metrics(
    probabilities: list[float], outcomes: list[int], category: CategoryId
) -> CategoryMetrics:
    """Compute metrics for a single category."""
    if not probabilities:
        return CategoryMetrics(
            category=category,
            brier_score=0.0,
            log_loss=0.0,
            ece=0.0,
            sample_count=0,
            outcome_mean=0.0,
            predicted_mean=0.0,
        )
    return CategoryMetrics(
        category=category,
        brier_score=_brier_score(probabilities, outcomes),
        log_loss=_log_loss(probabilities, outcomes),
        ece=_expected_calibration_error(probabilities, outcomes),
        sample_count=len(probabilities),
        outcome_mean=sum(outcomes) / len(outcomes),
        predicted_mean=sum(probabilities) / len(probabilities),
    )


@dataclass(frozen=True)
class OODSnapshot:
    """Minimal snapshot for OOD testing."""

    category: CategoryId
    model_probability: float
    outcome: int  # 0 or 1


def run_oos_test(
    dataset: list[OODSnapshot],
    train_categories: list[CategoryId],
    test_categories: list[CategoryId],
) -> OODResult:
    """Train on specified categories, test on excluded ones.

    Args:
        dataset: Full dataset with category labels.
        train_categories: Categories used for training.
        test_categories: Categories held out for OOD testing.

    Returns:
        OODResult comparing in-distribution vs out-of-distribution performance.
    """
    train_probs, train_outcomes = [], []
    test_probs, test_outcomes = [], []

    train_cat_set = set(train_categories)
    test_cat_set = set(test_categories)

    for snap in dataset:
        if snap.category in train_cat_set:
            train_probs.append(snap.model_probability)
            train_outcomes.append(snap.outcome)
        elif snap.category in test_cat_set:
            test_probs.append(snap.model_probability)
            test_outcomes.append(snap.outcome)

    train_metrics = _compute_category_metrics(
        train_probs, train_outcomes, ",".join(sorted(train_categories))
    )
    test_metrics = _compute_category_metrics(
        test_probs, test_outcomes, ",".join(sorted(test_categories))
    )

    brier_deg = test_metrics.brier_score - train_metrics.brier_score
    ll_deg = test_metrics.log_loss - train_metrics.log_loss
    ece_deg = test_metrics.ece - train_metrics.ece

    # Degradation is significant if OOD brier is >15% worse or >0.02 absolute
    is_degraded = (
        brier_deg > 0.02
        or (
            train_metrics.brier_score > 0
            and brier_deg / max(train_metrics.brier_score, 1e-10) > 0.15
        )
    )

    return OODResult(
        train_categories=train_categories,
        test_categories=test_categories,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        brier_degradation=brier_deg,
        log_loss_degradation=ll_deg,
        ece_degradation=ece_deg,
        is_degraded=is_degraded,
    )


def run_category_exclusion_ablation(
    dataset: list[OODSnapshot],
    categories: list[CategoryId],
) -> AblationReport:
    """Leave-one-out ablation: exclude each category and test OOD on it.

    For each category, trains on all others and evaluates on the excluded one.
    Identifies which categories are most important for generalization.
    """
    ablation_results: list[AblationResult] = []

    for excluded in categories:
        remaining = [c for c in categories if c != excluded]
        ood_result = run_oos_test(dataset, remaining, [excluded])

        # Importance = how much performance degrades when this category is the OOD target
        importance = ood_result.brier_degradation

        ablation_results.append(
            AblationResult(
                excluded_category=excluded,
                remaining_categories=remaining,
                ood_result=ood_result,
                category_importance=importance,
            )
        )

    if not ablation_results:
        return AblationReport(
            categories=categories,
            ablation_results=[],
            most_important_category="",
            least_important_category="",
            mean_ood_degradation=0.0,
            summary={},
        )

    sorted_results = sorted(ablation_results, key=lambda a: -a.category_importance)
    most_important = sorted_results[0].excluded_category
    least_important = sorted_results[-1].excluded_category
    mean_deg = sum(a.ood_result.brier_degradation for a in ablation_results) / len(
        ablation_results
    )
    summary = {a.excluded_category: a.category_importance for a in ablation_results}

    return AblationReport(
        categories=categories,
        ablation_results=ablation_results,
        most_important_category=most_important,
        least_important_category=least_important,
        mean_ood_degradation=mean_deg,
        summary=summary,
    )


# ── Feature-based OOD Detection ────────────────────────────────────────────


@dataclass(frozen=True)
class OODScore:
    """Out-of-domain score for a single observation."""

    observation_index: int
    score: float
    method: str
    is_ood: bool = False


@dataclass(frozen=True)
class OODScoreReport:
    """Report of feature-based OOD scores."""

    method: str
    scores: list[OODScore]
    threshold: float
    ood_fraction: float
    mean_score: float
    max_score: float

    def summary(self) -> dict:
        return {
            "method": self.method,
            "observation_count": len(self.scores),
            "threshold": self.threshold,
            "ood_fraction": self.ood_fraction,
            "mean_score": self.mean_score,
            "max_score": self.max_score,
        }


def _mahalanobis_scores(matrix: list[list[float]]) -> list[float]:
    """Compute Mahalanobis distance for each row from the distribution center.

    Uses the sample mean and covariance of the matrix.
    Falls back to Euclidean if covariance is singular.
    """
    n = len(matrix)
    if n == 0:
        return []
    d = len(matrix[0]) if matrix else 0
    if d == 0 or n < 2:
        return [0.0] * n

    # Compute mean vector
    means = [sum(row[i] for row in matrix) / n for i in range(d)]

    # Compute covariance matrix
    cov = [[0.0] * d for _ in range(d)]
    for i in range(d):
        for j in range(d):
            cov[i][j] = sum(
                (matrix[k][i] - means[i]) * (matrix[k][j] - means[j])
                for k in range(n)
            ) / (n - 1)

    # Compute inverse covariance (try Cholesky-like inversion, fallback to diagonal)
    try:
        inv_cov = _invert_matrix(cov, d)
    except (ZeroDivisionError, ValueError):
        # Fallback: use diagonal (variance only)
        inv_cov = [[0.0] * d for _ in range(d)]
        for i in range(d):
            var = cov[i][i]
            inv_cov[i][i] = 1.0 / var if var > 1e-15 else 1.0

    # Compute Mahalanobis distance for each observation
    scores = []
    for row in matrix:
        diff = [row[i] - means[i] for i in range(d)]
        # quadratic form: diff^T * inv_cov * diff
        quad = 0.0
        for i in range(d):
            for j in range(d):
                quad += diff[i] * inv_cov[i][j] * diff[j]
        scores.append(max(0.0, quad) ** 0.5)

    return scores


def _invert_matrix(mat: list[list[float]], n: int) -> list[list[float]]:
    """Invert a small matrix using Gauss-Jordan elimination."""
    # Create augmented matrix [A | I]
    aug = [list(mat[i]) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    for col in range(n):
        # Find pivot
        max_row = col
        for row in range(col + 1, n):
            if abs(aug[row][col]) > abs(aug[max_row][col]):
                max_row = row
        aug[col], aug[max_row] = aug[max_row], aug[col]

        pivot = aug[col][col]
        if abs(pivot) < 1e-15:
            raise ValueError("singular matrix")
        for j in range(2 * n):
            aug[col][j] /= pivot

        for row in range(n):
            if row != col:
                factor = aug[row][col]
                for j in range(2 * n):
                    aug[row][j] -= factor * aug[col][j]

    return [aug[i][n:] for i in range(n)]


def _isolation_forest_scores(matrix: list[list[float]], n_estimators: int = 100) -> list[float]:
    """Compute IsolationForest anomaly scores.

    Uses a pure-Python implementation: builds random trees and measures
    average path length for each observation. Shorter paths = more anomalous.
    """

    n = len(matrix)
    if n == 0:
        return []
    d = len(matrix[0]) if matrix else 0
    if d == 0:
        return [0.0] * n

    subsample_size = min(256, n)
    c = _average_path_length(subsample_size)

    rng = random.Random(42)
    path_lengths = [0.0] * n

    for _ in range(n_estimators):
        # Subsample
        indices = rng.sample(range(n), subsample_size)
        subset = [matrix[i] for i in indices]

        # Build a random tree
        tree = _build_isolation_tree(subset, d, rng, max_depth=10)

        # Compute path lengths for all points
        for idx in range(n):
            pl = _tree_path_length(tree, matrix[idx], 0, max_depth=20)
            path_lengths[idx] += pl

    # Average path lengths and normalize
    scores = []
    for i in range(n):
        avg_pl = path_lengths[i] / n_estimators
        # Anomaly score: 2^(-avg_pl / c) — higher = more anomalous
        score = 2.0 ** (-avg_pl / c) if c > 0 else 0.0
        scores.append(score)

    return scores


def _average_path_length(n: int) -> float:
    """Average path length in an isolation tree for n samples."""
    import math
    if n <= 1:
        return 0.0
    return 2.0 * (math.log(n - 1) + 0.5772156649) - 2.0 * (n - 1) / n


def _build_isolation_tree(
    data: list[list[float]], d: int, rng: random.Random, max_depth: int
) -> dict:
    """Build a single isolation tree node."""
    if len(data) <= 1 or max_depth <= 0:
        return {"type": "leaf", "size": len(data)}

    feature = rng.randint(0, d - 1)
    values = [row[feature] for row in data]
    lo, hi = min(values), max(values)

    if hi - lo < 1e-15:
        return {"type": "leaf", "size": len(data)}

    split = rng.uniform(lo, hi)
    left = [row for row in data if row[feature] < split]
    right = [row for row in data if row[feature] >= split]

    if not left or not right:
        return {"type": "leaf", "size": len(data)}

    return {
        "type": "internal",
        "feature": feature,
        "split": split,
        "left": _build_isolation_tree(left, d, rng, max_depth - 1),
        "right": _build_isolation_tree(right, d, rng, max_depth - 1),
    }


def _tree_path_length(tree: dict, point: list[float], depth: int, max_depth: int) -> float:
    """Compute path length of a point through the tree."""
    if tree["type"] == "leaf" or depth >= max_depth:
        return depth + _average_path_length(tree.get("size", 1))
    if point[tree["feature"]] < tree["split"]:
        return _tree_path_length(tree["left"], point, depth + 1, max_depth)
    return _tree_path_length(tree["right"], point, depth + 1, max_depth)


def compute_feature_ood_score(
    features_matrix: list[list[float]],
    method: str = "mahalanobis",
    threshold_percentile: float = 95.0,
) -> OODScoreReport:
    """Compute out-of-domain scores for observations using feature distance.

    Args:
        features_matrix: 2D list of feature vectors (n_obs x n_features).
        method: "mahalanobis" or "isolation_forest".
        threshold_percentile: Percentile of training scores to use as OOD threshold.

    Returns:
        OODScoreReport with per-observation scores and summary.
    """
    if not features_matrix:
        return OODScoreReport(
            method=method,
            scores=[],
            threshold=0.0,
            ood_fraction=0.0,
            mean_score=0.0,
            max_score=0.0,
        )

    if method == "mahalanobis":
        raw_scores = _mahalanobis_scores(features_matrix)
    elif method == "isolation_forest":
        raw_scores = _isolation_forest_scores(features_matrix)
    else:
        raise ValueError(f"unknown method: {method!r}, use 'mahalanobis' or 'isolation_forest'")

    # Compute threshold from training distribution
    sorted_scores = sorted(raw_scores)
    idx = int(len(sorted_scores) * threshold_percentile / 100.0)
    idx = min(idx, len(sorted_scores) - 1)
    threshold = sorted_scores[idx]

    scores = [
        OODScore(
            observation_index=i,
            score=s,
            method=method,
            is_ood=s > threshold,
        )
        for i, s in enumerate(raw_scores)
    ]

    ood_count = sum(1 for s in scores if s.is_ood)

    return OODScoreReport(
        method=method,
        scores=scores,
        threshold=threshold,
        ood_fraction=ood_count / len(scores),
        mean_score=sum(raw_scores) / len(raw_scores),
        max_score=max(raw_scores),
    )
