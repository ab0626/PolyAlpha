"""Market snapshot leakage tests for train/validation splits.

Part 27: Detects data leakage when the same market, event cluster, or
condition_id appears in both train and validation splits. Supports
temporal, market-grouped, event-grouped, and cluster-grouped splits.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

D = Decimal

SnapshotId: TypeAlias = str


@dataclass(frozen=True)
class MarketSnapshot:
    """Minimal snapshot interface for leakage detection."""

    snapshot_id: SnapshotId
    market_id: str
    event_id: str
    condition_id: str
    event_cluster: str
    category: str
    observation_timestamp: float  # epoch seconds for temporal splits


@dataclass(frozen=True)
class Violation:
    """A single detected leakage violation."""

    violation_type: str  # market, event_cluster, condition_id, temporal
    split_type: str  # market_grouped, event_grouped, cluster_grouped, temporal
    shared_entity: str
    train_ids: tuple[SnapshotId, ...]
    val_ids: tuple[SnapshotId, ...]
    severity: str  # low, medium, high, critical


@dataclass(frozen=True)
class LeakageReport:
    """Complete leakage detection report."""

    total_violations: int
    market_violations: int
    event_cluster_violations: int
    condition_id_violations: int
    temporal_violations: int
    severity_counts: dict[str, int]
    violations: list[Violation]
    is_clean: bool
    worst_severity: str

    def summary(self) -> dict:
        return {
            "total_violations": self.total_violations,
            "market_violations": self.market_violations,
            "event_cluster_violations": self.event_cluster_violations,
            "condition_id_violations": self.condition_id_violations,
            "temporal_violations": self.temporal_violations,
            "severity_counts": dict(self.severity_counts),
            "is_clean": self.is_clean,
            "worst_severity": self.worst_severity,
        }


@dataclass(frozen=True)
class SplitPerformance:
    """Performance metrics for a single split."""

    split_name: str
    brier_score: float
    log_loss: float
    sample_count: int
    row_level_brier: float | None = None
    grouped_level_brier: float | None = None


@dataclass(frozen=True)
class PerformanceReport:
    """Comparison of split performances flagging row-level-only effectiveness."""

    splits: list[SplitPerformance]
    row_level_only_works: bool
    grouped_fails: bool
    warning: str


_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def detect_train_val_leakage(
    snapshots: list[MarketSnapshot],
    train_ids: set[SnapshotId],
    val_ids: set[SnapshotId],
    temporal_threshold_seconds: float = 86400.0,
) -> LeakageReport:
    """Detect leakage between train and validation splits.

    Checks:
    1. Same market_id in both splits (different snapshots of same market)
    2. Same event_cluster in both splits
    3. Same condition_id in both splits
    4. Temporal leakage: val snapshot before latest train snapshot
    """
    train_snapshots = {s.snapshot_id: s for s in snapshots if s.snapshot_id in train_ids}
    val_snapshots = {s.snapshot_id: s for s in snapshots if s.snapshot_id in val_ids}

    violations: list[Violation] = []

    # Check 1: Same market in both splits
    violations.extend(_check_market_leakage(train_snapshots, val_snapshots))

    # Check 2: Same event cluster in both splits
    violations.extend(_check_cluster_leakage(train_snapshots, val_snapshots))

    # Check 3: Same condition_id in both splits
    violations.extend(_check_condition_leakage(train_snapshots, val_snapshots))

    # Check 4: Temporal leakage
    violations.extend(
        _check_temporal_leakage(train_snapshots, val_snapshots, temporal_threshold_seconds)
    )

    return _build_report(violations)


def _check_market_leakage(
    train: dict[SnapshotId, MarketSnapshot],
    val: dict[SnapshotId, MarketSnapshot],
) -> list[Violation]:
    """Find markets that appear in both train and val."""
    train_markets: dict[str, list[SnapshotId]] = defaultdict(list)
    val_markets: dict[str, list[SnapshotId]] = defaultdict(list)

    for sid, snap in train.items():
        train_markets[snap.market_id].append(sid)
    for sid, snap in val.items():
        val_markets[snap.market_id].append(sid)

    violations: list[Violation] = []
    shared_markets = set(train_markets.keys()) & set(val_markets.keys())
    for market_id in shared_markets:
        severity = _severity_from_count(
            len(train_markets[market_id]) + len(val_markets[market_id])
        )
        violations.append(
            Violation(
                violation_type="market",
                split_type="market_grouped",
                shared_entity=market_id,
                train_ids=tuple(train_markets[market_id]),
                val_ids=tuple(val_markets[market_id]),
                severity=severity,
            )
        )
    return violations


def _check_cluster_leakage(
    train: dict[SnapshotId, MarketSnapshot],
    val: dict[SnapshotId, MarketSnapshot],
) -> list[Violation]:
    """Find event clusters that appear in both train and val."""
    train_clusters: dict[str, list[SnapshotId]] = defaultdict(list)
    val_clusters: dict[str, list[SnapshotId]] = defaultdict(list)

    for sid, snap in train.items():
        if snap.event_cluster:
            train_clusters[snap.event_cluster].append(sid)
    for sid, snap in val.items():
        if snap.event_cluster:
            val_clusters[snap.event_cluster].append(sid)

    violations: list[Violation] = []
    shared = set(train_clusters.keys()) & set(val_clusters.keys())
    for cluster_id in shared:
        severity = _severity_from_count(
            len(train_clusters[cluster_id]) + len(val_clusters[cluster_id])
        )
        violations.append(
            Violation(
                violation_type="event_cluster",
                split_type="cluster_grouped",
                shared_entity=cluster_id,
                train_ids=tuple(train_clusters[cluster_id]),
                val_ids=tuple(val_clusters[cluster_id]),
                severity=severity,
            )
        )
    return violations


def _check_condition_leakage(
    train: dict[SnapshotId, MarketSnapshot],
    val: dict[SnapshotId, MarketSnapshot],
) -> list[Violation]:
    """Find condition_ids that appear in both train and val."""
    train_conds: dict[str, list[SnapshotId]] = defaultdict(list)
    val_conds: dict[str, list[SnapshotId]] = defaultdict(list)

    for sid, snap in train.items():
        train_conds[snap.condition_id].append(sid)
    for sid, snap in val.items():
        val_conds[snap.condition_id].append(sid)

    violations: list[Violation] = []
    shared = set(train_conds.keys()) & set(val_conds.keys())
    for cond_id in shared:
        severity = _severity_from_count(
            len(train_conds[cond_id]) + len(val_conds[cond_id])
        )
        violations.append(
            Violation(
                violation_type="condition_id",
                split_type="market_grouped",
                shared_entity=cond_id,
                train_ids=tuple(train_conds[cond_id]),
                val_ids=tuple(val_conds[cond_id]),
                severity=severity,
            )
        )
    return violations


def _check_temporal_leakage(
    train: dict[SnapshotId, MarketSnapshot],
    val: dict[SnapshotId, MarketSnapshot],
    threshold: float,
) -> list[Violation]:
    """Detect temporal leakage: val snapshot before latest train for same market."""
    train_by_market: dict[str, list[float]] = defaultdict(list)
    for snap in train.values():
        train_by_market[snap.market_id].append(snap.observation_timestamp)

    violations: list[Violation] = []
    for sid, snap in val.items():
        train_times = train_by_market.get(snap.market_id, [])
        if not train_times:
            continue
        latest_train = max(train_times)
        if snap.observation_timestamp < latest_train - threshold:
            violations.append(
                Violation(
                    violation_type="temporal",
                    split_type="temporal",
                    shared_entity=snap.market_id,
                    train_ids=(),
                    val_ids=(sid,),
                    severity=(
                        "high"
                        if latest_train - snap.observation_timestamp
                        > threshold * 3
                        else "medium"
                    ),
                )
            )
    return violations


def _severity_from_count(count: int) -> str:
    """Map entity overlap count to severity."""
    if count <= 2:
        return "low"
    if count <= 5:
        return "medium"
    if count <= 20:
        return "high"
    return "critical"


def _build_report(violations: list[Violation]) -> LeakageReport:
    """Build the final leakage report."""
    severity_counts: dict[str, int] = defaultdict(int)
    market_count = cluster_count = condition_count = temporal_count = 0

    for v in violations:
        severity_counts[v.severity] += 1
        if v.violation_type == "market":
            market_count += 1
        elif v.violation_type == "event_cluster":
            cluster_count += 1
        elif v.violation_type == "condition_id":
            condition_count += 1
        elif v.violation_type == "temporal":
            temporal_count += 1

    worst = "low"
    for sev, cnt in severity_counts.items():
        if cnt > 0 and _SEVERITY_ORDER.get(sev, 0) > _SEVERITY_ORDER.get(worst, 0):
            worst = sev

    return LeakageReport(
        total_violations=len(violations),
        market_violations=market_count,
        event_cluster_violations=cluster_count,
        condition_id_violations=condition_count,
        temporal_violations=temporal_count,
        severity_counts=dict(severity_counts),
        violations=violations,
        is_clean=len(violations) == 0,
        worst_severity=worst if violations else "none",
    )


def compare_split_performance(
    results_by_split: dict[str, SplitPerformance],
) -> PerformanceReport:
    """Flag if model only works under row-level splitting.

    Row-level splitting means individual snapshots are randomly assigned,
    which can leak information when multiple snapshots share a market.
    If row-level performance is good but grouped-level performance degrades,
    the model may be relying on leaked information.
    """
    splits = list(results_by_split.values())
    row_level = [s for s in splits if "row" in s.split_name.lower()]
    grouped = [
        s for s in splits
        if "group" in s.split_name.lower()
        or "cluster" in s.split_name.lower()
    ]

    row_brier = [s.brier_score for s in row_level if s.brier_score is not None]
    grouped_brier = [s.brier_score for s in grouped if s.brier_score is not None]

    row_mean = sum(row_brier) / len(row_brier) if row_brier else None
    grouped_mean = sum(grouped_brier) / len(grouped_brier) if grouped_brier else None

    row_only_works = (
        row_mean is not None
        and grouped_mean is not None
        and row_mean < grouped_mean * 0.85
    )
    grouped_fails = grouped_mean is not None and grouped_mean > 0.35

    warning = ""
    if row_only_works:
        warning = (
            "WARNING: Model performs significantly better under row-level splitting "
            f"(brier={row_mean:.4f}) than grouped splitting (brier={grouped_mean:.4f}). "
            "This suggests the model may be exploiting leaked information."
        )
    elif grouped_fails:
        warning = (
            f"WARNING: Grouped splitting shows poor performance (brier={grouped_mean:.4f}). "
            "Model may not generalize to unseen markets."
        )

    return PerformanceReport(
        splits=splits,
        row_level_only_works=row_only_works,
        grouped_fails=grouped_fails,
        warning=warning,
    )
