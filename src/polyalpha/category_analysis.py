"""Category analysis — per-category model performance breakdown.

Section 12 of the v0.3 spec: which market categories does the model
perform best/worst in? Reveals domain-specific strengths.
"""

from collections import defaultdict
from dataclasses import dataclass

from .research_dataset import ResearchDataset


@dataclass(frozen=True)
class CategoryResult:
    """Performance metrics for a single category."""

    category: str
    brier_score: float
    market_brier: float
    delta_brier: float
    log_loss: float
    directional_accuracy: float
    calibration_error: float
    mean_model_prob: float
    mean_outcome: float
    fill_rate: float
    count: int
    cluster_count: int


@dataclass(frozen=True)
class CategoryAnalysis:
    """Full category analysis."""

    categories: list[CategoryResult]
    best_category: str
    worst_category: str
    overall_delta_brier: float

    def summary(self) -> dict:
        return {
            "category_count": len(self.categories),
            "best_category": self.best_category,
            "worst_category": self.worst_category,
            "overall_delta_brier": self.overall_delta_brier,
        }


def _log_loss_val(probs: list[float], outcomes: list[int]) -> float:
    import math

    if not probs:
        return 10.0
    eps = 1e-15
    total = 0.0
    for p, o in zip(probs, outcomes):
        p = max(eps, min(1.0 - eps, p))
        total -= math.log(p if o == 1 else 1.0 - p)
    return total / len(probs)


def _ece_val(probs: list[float], outcomes: list[int], n_buckets: int = 10) -> float:
    if not probs:
        return 1.0
    buckets: dict[int, list[tuple[float, int]]] = {}
    for p, o in zip(probs, outcomes):
        b = min(int(p * n_buckets), n_buckets - 1)
        buckets.setdefault(b, []).append((p, o))
    total_error = 0.0
    total_count = 0
    for b in range(n_buckets):
        items = buckets.get(b, [])
        if not items:
            continue
        mean_pred = sum(p for p, _ in items) / len(items)
        mean_actual = sum(o for _, o in items) / len(items)
        total_error += abs(mean_pred - mean_actual) * len(items)
        total_count += len(items)
    return total_error / total_count if total_count else 1.0


def analyze_categories(dataset: ResearchDataset) -> CategoryAnalysis:
    """Analyze model performance per category."""
    resolved = [s for s in dataset.snapshots if s.final_resolution is not None]
    if not resolved:
        return CategoryAnalysis(
            categories=[], best_category="", worst_category="", overall_delta_brier=0.0
        )

    # Group by category
    by_cat: dict[str, list] = defaultdict(list)
    for snap in resolved:
        by_cat[snap.category].append(snap)

    categories = []
    for cat, recs in sorted(by_cat.items()):
        model_probs = [float(s.model_probability) for s in recs]
        market_probs = [float(s.yes_mid) if s.yes_mid else float(s.model_probability) for s in recs]
        outcomes = [s.final_resolution for s in recs]

        model_b = sum((p - o) ** 2 for p, o in zip(model_probs, outcomes)) / len(model_probs)
        market_b = sum((p - o) ** 2 for p, o in zip(market_probs, outcomes)) / len(market_probs)

        correct = sum(1 for p, o in zip(model_probs, outcomes) if (p >= 0.5) == (o == 1))
        direction = correct / len(model_probs) if model_probs else 0.0

        clusters = len(set(s.event_cluster for s in recs))
        fill_rate = sum(1 for s in recs if s.execution_price is not None) / len(recs)

        categories.append(
            CategoryResult(
                category=cat,
                brier_score=round(model_b, 6),
                market_brier=round(market_b, 6),
                delta_brier=round(market_b - model_b, 6),
                log_loss=round(_log_loss_val(model_probs, outcomes), 6),
                directional_accuracy=round(direction, 4),
                calibration_error=round(_ece_val(model_probs, outcomes), 6),
                mean_model_prob=round(sum(model_probs) / len(model_probs), 4),
                mean_outcome=round(sum(outcomes) / len(outcomes), 4),
                fill_rate=round(fill_rate, 4),
                count=len(recs),
                cluster_count=clusters,
            )
        )

    best = max(categories, key=lambda c: c.delta_brier)
    worst = min(categories, key=lambda c: c.delta_brier)
    total_model = sum(c.brier_score * c.count for c in categories)
    total_market = sum(c.market_brier * c.count for c in categories)
    total_n = sum(c.count for c in categories)
    overall_delta = (total_market - total_model) / total_n if total_n else 0.0

    return CategoryAnalysis(
        categories=categories,
        best_category=best.category,
        worst_category=worst.category,
        overall_delta_brier=round(overall_delta, 6),
    )
