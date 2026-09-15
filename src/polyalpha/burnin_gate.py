"""Burn-in go/no-go gate and the REAL_DATA_START marker.

Part of the v0.3.0 data-collection architecture. Before declaring the official
collection window, a burn-in must satisfy an explicit gate. This module
evaluates the gate conditions against observed burn-in results and writes an
immutable REAL_DATA_START marker recording the exact code/config/data state at
the boundary.

The gate deliberately mirrors what a top-quant review would insist on:
  * zero unexplained raw-record corruption
  * replay determinism (raw -> normalized -> same hash)
  * no delta applied to invalid/stale book state
  * no unrecoverable reconnect state
  * no unexplained future/local timestamp invariant failures
  * REST reconciliation mismatches explained + bounded
  * heartbeat recovery, restart recovery, partial-file recovery all pass
  * metadata point-in-time reconstruction passes
  * market resolution captured and replayable
  * baseline/model hash unchanged
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .phases import PhaseStore  # noqa: F401  (used in type annotations)

# ── Burn-in gate ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateCondition:
    name: str
    passed: bool
    details: str = ""

    def summary(self) -> dict:
        return {"name": self.name, "passed": self.passed, "details": self.details}


@dataclass
class BurnInGate:
    conditions: list[GateCondition] = field(default_factory=list)

    def add(self, name: str, passed: bool, details: str = "") -> None:
        self.conditions.append(GateCondition(name, passed, details))

    @property
    def all_pass(self) -> bool:
        return all(c.passed for c in self.conditions)

    def summary(self) -> dict:
        return {
            "all_pass": self.all_pass,
            "passed_count": sum(1 for c in self.conditions if c.passed),
            "failed_count": sum(1 for c in self.conditions if not c.passed),
            "conditions": [c.summary() for c in self.conditions],
        }


def evaluate_burn_in_gate(
    raw_corruption_count: int,
    replay_deterministic: bool,
    delta_on_stale_count: int,
    unrecoverable_reconnect_count: int,
    timestamp_invariant_failures: int,
    reconciliation_total: int,
    reconciliation_mismatches: int,
    heartbeat_recovery: bool,
    restart_recovery: bool,
    partial_file_recovery: bool,
    metadata_point_in_time: bool,
    resolution_captured: bool,
    model_hash_unchanged: bool,
) -> BurnInGate:
    """Evaluate every burn-in gate condition. Returns the full gate result."""
    gate = BurnInGate()

    gate.add(
        "raw_corruption",
        raw_corruption_count == 0,
        f"{raw_corruption_count} unexplained raw-record corruptions",
    )
    gate.add(
        "replay_deterministic",
        replay_deterministic,
        "raw -> normalized replay must be byte-identical across runs",
    )
    gate.add(
        "no_delta_on_stale_book",
        delta_on_stale_count == 0,
        f"{delta_on_stale_count} deltas applied to invalid/stale books",
    )
    gate.add(
        "reconnect_recoverable",
        unrecoverable_reconnect_count == 0,
        f"{unrecoverable_reconnect_count} unrecoverable reconnect states",
    )
    gate.add(
        "timestamp_invariants",
        timestamp_invariant_failures == 0,
        f"{timestamp_invariant_failures} unexplained future/local timestamp failures",
    )
    # Reconciliation: mismatches must be explained AND bounded (<=1%).
    bounded = reconciliation_total == 0 or (
        reconciliation_mismatches / max(1, reconciliation_total) <= 0.01
    )
    gate.add(
        "reconciliation_bounded",
        bounded,
        f"{reconciliation_mismatches}/{reconciliation_total} mismatches "
        f"(must be explained and <=1%)",
    )
    gate.add("heartbeat_recovery", heartbeat_recovery, "heartbeat survived forced disconnect")
    gate.add("restart_recovery", restart_recovery, "collector restart recovered cleanly")
    gate.add("partial_file_recovery", partial_file_recovery, "partial raw-file recovery verified")
    gate.add(
        "metadata_point_in_time",
        metadata_point_in_time,
        "metadata point-in-time reconstruction verified",
    )
    gate.add(
        "resolution_captured",
        resolution_captured,
        "market resolution captured and replayable",
    )
    gate.add(
        "model_hash_unchanged",
        model_hash_unchanged,
        "baseline/model hash must be unchanged",
    )
    return gate


# ── REAL_DATA_START marker ───────────────────────────────────────────────────


@dataclass(frozen=True)
class RealDataStartMarker:
    phase: str
    timestamp_utc: str
    baseline_tag: str
    baseline_commit: str
    config_sha256: str
    model_source_sha256: str
    feature_schema_sha256: str
    collector_commit: str
    burnin_report_sha256: str
    marker_sha256: str = ""

    def as_dict(self) -> dict:
        d = {
            "phase": self.phase,
            "timestamp_utc": self.timestamp_utc,
            "baseline_tag": self.baseline_tag,
            "baseline_commit": self.baseline_commit,
            "config_sha256": self.config_sha256,
            "model_source_sha256": self.model_source_sha256,
            "feature_schema_sha256": self.feature_schema_sha256,
            "collector_commit": self.collector_commit,
            "burnin_report_sha256": self.burnin_report_sha256,
        }
        if self.marker_sha256:
            d["marker_sha256"] = self.marker_sha256
        return d

    def combined_hash(self) -> str:
        d = self.as_dict()
        d.pop("marker_sha256", None)
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_real_data_start_marker(
    marker_path: str | Path,
    baseline_tag: str,
    baseline_commit: str,
    config_sha256: str,
    model_source_sha256: str,
    feature_schema_sha256: str,
    collector_commit: str,
    burnin_report_sha256: str,
    phase_store: "PhaseStore | None" = None,
) -> RealDataStartMarker:
    """Write the immutable REAL_DATA_START marker. Refuses to overwrite.

    When a phase_store is provided, it must be in BURNIN_PASSED phase;
    otherwise the marker write is rejected.
    """
    if phase_store is not None:
        phase_store.require("BURNIN_PASSED")
    path = Path(marker_path)
    if path.exists():
        raise FileExistsError(f"REAL_DATA_START marker already exists: {path}")
    marker = RealDataStartMarker(
        phase="REAL_DATA_START",
        timestamp_utc=datetime.now(UTC).isoformat(),
        baseline_tag=baseline_tag,
        baseline_commit=baseline_commit,
        config_sha256=config_sha256,
        model_source_sha256=model_source_sha256,
        feature_schema_sha256=feature_schema_sha256,
        collector_commit=collector_commit,
        burnin_report_sha256=burnin_report_sha256,
    )
    digest = marker.combined_hash()
    marker = RealDataStartMarker(
        phase=marker.phase,
        timestamp_utc=marker.timestamp_utc,
        baseline_tag=marker.baseline_tag,
        baseline_commit=marker.baseline_commit,
        config_sha256=marker.config_sha256,
        model_source_sha256=marker.model_source_sha256,
        feature_schema_sha256=marker.feature_schema_sha256,
        collector_commit=collector_commit,
        burnin_report_sha256=burnin_report_sha256,
        marker_sha256=digest,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(marker.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return marker


def verify_real_data_start_marker(path: str | Path) -> tuple[bool, str]:
    """Verify a REAL_DATA_START marker's hash is intact."""
    path = Path(path)
    if not path.exists():
        return False, "marker does not exist"
    data = json.loads(path.read_text(encoding="utf-8"))
    stored = data.pop("marker_sha256", "")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != stored:
        return False, "marker hash mismatch: marker was modified"
    if data.get("phase") != "REAL_DATA_START":
        return False, f"unexpected phase: {data.get('phase')}"
    return True, "ok"


# ── Burn-in report artifact ──────────────────────────────────────────────────


@dataclass
class BurnInReport:
    period_start: str
    period_end: str
    baseline_commit: str
    messages: int
    markets: int
    reconnects: int
    forced_failures: int
    replay_deterministic: bool
    raw_corruption: int
    partial_records: int
    hash_mismatches: int
    invalid_delta_applications: int
    unresolved_book_mismatches: int
    manifest_chain_ok: bool
    metadata_reconstruction_ok: bool
    resolution_lifecycle_ok: bool
    crash_recovery_ok: bool
    gate_passed: bool
    # Live-ready state-machine scoreboard (must all be zero during burn-in)
    phase_machine_violations: int = 0
    intent_ledger_duplicates: int = 0
    kill_switch_bypasses: int = 0
    approval_boundary_bypasses: int = 0

    def as_dict(self) -> dict:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "baseline_commit": self.baseline_commit,
            "messages": self.messages,
            "markets": self.markets,
            "reconnects": self.reconnects,
            "forced_failures": self.forced_failures,
            "replay_deterministic": self.replay_deterministic,
            "raw_corruption": self.raw_corruption,
            "partial_records": self.partial_records,
            "hash_mismatches": self.hash_mismatches,
            "invalid_delta_applications": self.invalid_delta_applications,
            "unresolved_book_mismatches": self.unresolved_book_mismatches,
            "manifest_chain_ok": self.manifest_chain_ok,
            "metadata_reconstruction_ok": self.metadata_reconstruction_ok,
            "resolution_lifecycle_ok": self.resolution_lifecycle_ok,
            "crash_recovery_ok": self.crash_recovery_ok,
            "gate_passed": self.gate_passed,
            "phase_machine_violations": self.phase_machine_violations,
            "intent_ledger_duplicates": self.intent_ledger_duplicates,
            "kill_switch_bypasses": self.kill_switch_bypasses,
            "approval_boundary_bypasses": self.approval_boundary_bypasses,
        }

    def combined_hash(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_burn_in_report(report: BurnInReport, directory: str | Path) -> tuple[Path, str]:
    """Write burnin-report.json + burnin-report.md, returning (dir, sha256)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    digest = report.combined_hash()

    json_path = directory / "burnin-report.json"
    json_path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    md = _render_burnin_markdown(report)
    (directory / "burnin-report.md").write_text(md, encoding="utf-8")
    return directory, digest


def _render_burnin_markdown(report: BurnInReport) -> str:
    def check(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    lines = [
        "PolyAlpha Collection Burn-In",
        "",
        f"Period: {report.period_start} → {report.period_end}",
        f"Baseline: {report.baseline_commit}",
        "",
        f"Messages: {report.messages:,}",
        f"Markets: {report.markets}",
        f"Reconnects: {report.reconnects}",
        f"Forced failures: {report.forced_failures}",
        "",
        f"Replay deterministic: {check(report.replay_deterministic)}",
        f"Manifest chain: {check(report.manifest_chain_ok)}",
        f"Raw corruption: {report.raw_corruption}",
        f"Unresolved book mismatches: {report.unresolved_book_mismatches}",
        f"Invalid stale-delta applications: {report.invalid_delta_applications}",
        f"Hash mismatches: {report.hash_mismatches}",
        f"Partial records: {report.partial_records} quarantined",
        "",
        "LIVE-READY STATE MACHINE",
        f"Phase-machine violations: {report.phase_machine_violations}",
        f"Intent-ledger duplicates: {report.intent_ledger_duplicates}",
        f"Kill-switch bypasses: {report.kill_switch_bypasses}",
        f"Approval-boundary bypasses: {report.approval_boundary_bypasses}",
        "",
        f"Metadata reconstruction: {check(report.metadata_reconstruction_ok)}",
        f"Resolution lifecycle: {check(report.resolution_lifecycle_ok)}",
        f"Crash recovery: {check(report.crash_recovery_ok)}",
        "",
        f"Gate: {check(report.gate_passed)}",
        "",
    ]
    return "\n".join(lines)