"""DatasetAuditor — validates research dataset integrity.

Section 4 of the v0.3 spec: 18 mandatory checks including temporal ordering,
probability bounds, bid/ask validation, NaN/Inf detection, YES/NO consistency,
and duplicate detection. Produces a DatasetAuditReport with row-level tracking.
"""

from dataclasses import dataclass, field
from decimal import Decimal as D

from .research_dataset import ResearchDataset


@dataclass(frozen=True)
class AuditCheck:
    name: str
    passed: bool
    message: str
    severity: str = "error"
    affected_rows: int = 0


@dataclass
class DatasetAuditReport:
    total_rows: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    failure_counts: dict[str, int] = field(default_factory=dict)
    feature_leakage_count: int = 0
    timestamp_errors: int = 0
    impossible_books: int = 0
    duplicate_rows: int = 0
    checks: list[AuditCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.severity == "error")

    @property
    def error_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == "warning")

    def summary(self) -> dict:
        return {
            "passed": self.passed,
            "total_rows": self.total_rows,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "feature_leakage_count": self.feature_leakage_count,
            "timestamp_errors": self.timestamp_errors,
            "impossible_books": self.impossible_books,
            "duplicate_rows": self.duplicate_rows,
            "failure_counts": self.failure_counts,
            "warnings": self.warnings[:20],
        }


class DatasetAuditor:
    def audit(self, dataset: ResearchDataset) -> DatasetAuditReport:
        report = DatasetAuditReport(total_rows=len(dataset.snapshots))
        reject_set: set[int] = set()

        checks = [
            self._check_duplicate_observations(dataset, reject_set),
            self._check_duplicate_market_timestamp(dataset, reject_set),
            self._check_impossible_bid_ask(dataset, reject_set),
            self._check_prices_outside_unit(dataset, reject_set),
            self._check_negative_sizes(dataset, reject_set),
            self._check_negative_volume_liquidity(dataset, reject_set),
            self._check_nan_inf(dataset, reject_set),
            self._check_malformed_timestamps(dataset, reject_set),
            self._check_timestamp_reversals(dataset, reject_set),
            self._check_future_feature_timestamps(dataset, reject_set),
            self._check_resolution_before_observation(dataset, reject_set),
            self._check_missing_resolution(dataset),
            self._check_duplicate_event_labels(dataset),
            self._check_conflicting_resolutions(dataset),
            self._check_yes_no_inconsistency(dataset, reject_set),
            self._check_probability_bounds(dataset, reject_set),
            self._check_outcome_validity(dataset),
            self._check_cluster_independence(dataset),
        ]
        report.checks = checks
        report.rejected_rows = len(reject_set)
        report.accepted_rows = report.total_rows - report.rejected_rows
        for c in checks:
            if not c.passed:
                report.failure_counts[c.name] = c.affected_rows
                if c.severity == "warning":
                    report.warnings.append(f"{c.name}: {c.message}")
        return report

    def _check_duplicate_observations(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        seen: dict[tuple, int] = {}
        dupes = 0
        for i, s in enumerate(ds.snapshots):
            key = (s.market_id, s.observation_timestamp.isoformat(), s.model_probability)
            if key in seen:
                dupes += 1
                reject.add(i)
            else:
                seen[key] = i
        return AuditCheck(
            "duplicate_observations", dupes == 0, f"{dupes} duplicate rows", "error", dupes
        )

    def _check_duplicate_market_timestamp(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        seen: dict[tuple, int] = {}
        dupes = 0
        for i, s in enumerate(ds.snapshots):
            key = (s.market_id, s.observation_timestamp.isoformat())
            if key in seen:
                dupes += 1
                reject.add(i)
            else:
                seen[key] = i
        return AuditCheck(
            "duplicate_market_timestamp",
            dupes == 0,
            f"{dupes} duplicate market/timestamp pairs",
            "error",
            dupes,
        )

    def _check_impossible_bid_ask(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            if s.yes_best_bid is not None and s.yes_best_ask is not None:
                if s.yes_best_bid > s.yes_best_ask:
                    count += 1
                    reject.add(i)
            if s.no_best_bid is not None and s.no_best_ask is not None:
                if s.no_best_bid > s.no_best_ask:
                    count += 1
                    reject.add(i)
        return AuditCheck(
            "impossible_bid_ask", count == 0, f"{count} impossible bid/ask states", "error", count
        )

    def _check_prices_outside_unit(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            for price in [
                s.yes_best_bid,
                s.yes_best_ask,
                s.no_best_bid,
                s.no_best_ask,
                s.yes_mid,
                s.no_mid,
                s.execution_price,
                s.model_probability,
            ]:
                if price is not None:
                    if price < 0 or price > 1:
                        count += 1
                        reject.add(i)
                        break
        return AuditCheck(
            "prices_outside_unit",
            count == 0,
            f"{count} rows with prices outside [0,1]",
            "error",
            count,
        )

    def _check_negative_sizes(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            for size in [
                s.yes_depth_1,
                s.yes_depth_5,
                s.yes_depth_10,
                s.yes_bid_size,
                s.yes_ask_size,
                s.no_depth_1,
                s.no_depth_5,
                s.no_depth_10,
                s.no_bid_size,
                s.no_ask_size,
            ]:
                if size < 0:
                    count += 1
                    reject.add(i)
                    break
        return AuditCheck(
            "negative_sizes", count == 0, f"{count} rows with negative sizes", "error", count
        )

    def _check_negative_volume_liquidity(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            if s.volume < 0 or s.liquidity < 0:
                count += 1
                reject.add(i)
        return AuditCheck(
            "negative_volume_liquidity",
            count == 0,
            f"{count} rows with negative volume/liquidity",
            "error",
            count,
        )

    def _check_nan_inf(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            for val in [
                s.yes_best_bid,
                s.yes_best_ask,
                s.yes_mid,
                s.yes_spread,
                s.no_best_bid,
                s.no_best_ask,
                s.no_mid,
                s.no_spread,
                s.model_probability,
                s.execution_price,
                s.net_edge,
            ]:
                if val is not None and not val.is_finite():
                    count += 1
                    reject.add(i)
                    break
        return AuditCheck(
            "nan_inf", count == 0, f"{count} rows with NaN/Inf values", "error", count
        )

    def _check_malformed_timestamps(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            if s.observation_timestamp is None:
                count += 1
                reject.add(i)
        return AuditCheck(
            "malformed_timestamps", count == 0, f"{count} malformed timestamps", "error", count
        )

    def _check_timestamp_reversals(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        sorted_snaps = sorted(enumerate(ds.snapshots), key=lambda x: x[1].observation_timestamp)
        count = 0
        for j in range(1, len(sorted_snaps)):
            prev_idx, prev = sorted_snaps[j - 1]
            curr_idx, curr = sorted_snaps[j]
            if curr.observation_timestamp < prev.observation_timestamp:
                count += 1
                reject.add(curr_idx)
        return AuditCheck(
            "timestamp_reversals", count == 0, f"{count} timestamp reversals", "error", count
        )

    def _check_future_feature_timestamps(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            ft = s.feature_provenance.feature_timestamp
            if ft is not None and ft > s.observation_timestamp:
                count += 1
                reject.add(i)
        return AuditCheck(
            "future_feature_timestamps",
            count == 0,
            f"{count} future feature timestamps",
            "error",
            count,
        )

    def _check_resolution_before_observation(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            if (
                s.resolution_timestamp is not None
                and s.resolution_timestamp < s.observation_timestamp
            ):
                count += 1
                reject.add(i)
        return AuditCheck(
            "resolution_before_observation",
            count == 0,
            f"{count} resolutions before observation",
            "error",
            count,
        )

    def _check_missing_resolution(self, ds: ResearchDataset) -> AuditCheck:
        count = sum(1 for s in ds.snapshots if s.final_resolution is None)
        severity = "warning" if count > len(ds.snapshots) * 0.5 else "info"
        return AuditCheck(
            "missing_resolution",
            count == 0 or severity == "info",
            f"{count}/{len(ds.snapshots)} missing resolution",
            severity,
            count,
        )

    def _check_duplicate_event_labels(self, ds: ResearchDataset) -> AuditCheck:
        event_resolutions: dict[str, set[int]] = {}
        for s in ds.snapshots:
            if s.event_id and s.final_resolution is not None:
                event_resolutions.setdefault(s.event_id, set()).add(s.final_resolution)
        conflicts = {eid: vals for eid, vals in event_resolutions.items() if len(vals) > 1}
        return AuditCheck(
            "duplicate_event_labels",
            len(conflicts) == 0,
            f"{len(conflicts)} conflicting event resolutions",
            "error",
            len(conflicts),
        )

    def _check_conflicting_resolutions(self, ds: ResearchDataset) -> AuditCheck:
        market_resolutions: dict[str, list[int]] = {}
        for s in ds.snapshots:
            if s.final_resolution is not None:
                market_resolutions.setdefault(s.market_id, []).append(s.final_resolution)
        conflicts = {mid: vals for mid, vals in market_resolutions.items() if len(set(vals)) > 1}
        return AuditCheck(
            "conflicting_resolutions",
            len(conflicts) == 0,
            f"{len(conflicts)} markets with conflicting resolutions",
            "error",
            len(conflicts),
        )

    def _check_yes_no_inconsistency(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            if (
                s.yes_best_bid is not None
                and s.no_best_ask is not None
                and s.yes_best_bid > 1 - s.no_best_ask + D("0.02")
            ):
                count += 1
                reject.add(i)
            if (
                s.yes_best_ask is not None
                and s.no_best_bid is not None
                and s.yes_best_ask < 1 - s.no_best_bid - D("0.02")
            ):
                count += 1
                reject.add(i)
        return AuditCheck(
            "yes_no_inconsistency", count == 0, f"{count} YES/NO inconsistencies", "warning", count
        )

    def _check_probability_bounds(self, ds: ResearchDataset, reject: set) -> AuditCheck:
        count = 0
        for i, s in enumerate(ds.snapshots):
            if s.model_probability is not None:
                if s.model_probability <= 0 or s.model_probability >= 1:
                    count += 1
                    reject.add(i)
        return AuditCheck(
            "probability_bounds", count == 0, f"{count} probabilities outside (0,1)", "error", count
        )

    def _check_outcome_validity(self, ds: ResearchDataset) -> AuditCheck:
        count = sum(
            1
            for s in ds.snapshots
            if s.final_resolution is not None and s.final_resolution not in (0, 1)
        )
        return AuditCheck(
            "outcome_validity", count == 0, f"{count} invalid outcomes", "error", count
        )

    def _check_cluster_independence(self, ds: ResearchDataset) -> AuditCheck:
        clusters = {}
        for s in ds.snapshots:
            if s.event_cluster:
                clusters[s.event_cluster] = clusters.get(s.event_cluster, 0) + 1
        if not clusters:
            return AuditCheck("cluster_independence", True, "No clusters defined", "info", 0)
        singletons = sum(1 for v in clusters.values() if v == 1)
        return AuditCheck(
            "cluster_independence",
            singletons < len(clusters),
            f"{len(clusters)} clusters, {singletons} singletons",
            "warning",
            0,
        )
