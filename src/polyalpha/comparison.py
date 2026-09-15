"""Paired out-of-sample comparisons with cluster-level resampling and coverage counts."""

import random
from collections import defaultdict

from .calibration import metrics


def summarize_folds(folds, bootstrap_samples=2000, seed=417, minimum_clusters=20):
    if bootstrap_samples < 1 or minimum_clusters < 2:
        raise ValueError("invalid bootstrap settings")
    rows = [
        row
        for fold in folds
        for row in fold.get("prediction_ledger", [])
        if row["outcome"] is not None
    ]
    if len({row["market_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate evaluation markets across folds")
    summary = dict(
        candidate_markets=sum(f["candidate_markets"] for f in folds),
        unresolved_markets=sum(f["unresolved_markets"] for f in folds),
        scored_markets=len(rows),
        skipped_folds=sum(f["status"] == "insufficient_data" for f in folds),
    )
    if not rows:
        return dict(
            summary, status="no_scored_observations", raw_metrics=None, calibrated_metrics=None
        )
    raw = metrics([r["raw_probability"] for r in rows], [r["outcome"] for r in rows])
    calibrated = metrics([r["calibrated_probability"] for r in rows], [r["outcome"] for r in rows])
    by_cluster = defaultdict(list)
    for row in rows:
        difference = (row["calibrated_probability"] - row["outcome"]) ** 2 - (
            row["raw_probability"] - row["outcome"]
        ) ** 2
        by_cluster[row["cluster"]].append(difference)
    summary.update(
        status="evaluated",
        raw_metrics=raw,
        calibrated_metrics=calibrated,
        cluster_count=len(by_cluster),
        brier_change=calibrated["brier"] - raw["brier"],
        interpretation=(
            "negative Brier change favors calibration;"
            " this is not trading profitability"
        ),
    )
    if len(by_cluster) < minimum_clusters:
        summary["cluster_bootstrap"] = dict(
            status="insufficient_clusters", required=minimum_clusters
        )
        return summary
    rng = random.Random(seed)
    clusters = sorted(by_cluster)
    statistics = []
    for _ in range(bootstrap_samples):
        chosen = rng.choices(clusters, k=len(clusters))
        differences = [x for cluster in chosen for x in by_cluster[cluster]]
        statistics.append(sum(differences) / len(differences))
    statistics.sort()
    summary["cluster_bootstrap"] = dict(
        status="estimated",
        samples=bootstrap_samples,
        seed=seed,
        lower=statistics[int(0.025 * (bootstrap_samples - 1))],
        upper=statistics[int(0.975 * (bootstrap_samples - 1))],
        assumptions=(
            "percentile interval resampling whole declared clusters;"
            " dependence between clusters is not established absent"
        ),
    )
    return summary
