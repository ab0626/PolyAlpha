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

# Hard qualifying requirements for a burn-in that may authorize REAL_DATA_START.
# These are the frozen research baseline identity plus eligibility flags.
QUALIFYING_BASELINE_COMMIT = "334b911"
QUALIFYING_BASELINE_TAG = "v0.3.0-research-baseline-334b911"
QUALIFYING_RESEARCH_LOGIC_SHA256 = (
    "df71b8f387e146681cea47639f6d61f79f82dff29012913c8124346d4afb598e"
)

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


def zero_tolerance_failures(report: "BurnInReport") -> list[str]:
    """Assert the absolute zero-tolerance criteria.

    These counters MUST be zero for the gate to pass:
      raw hash mismatches, wire-fidelity failures, manifest-chain failures,
      unresolved REST/book mismatches, stale-delta applications, replay A/B
      differences, phase-machine violations, intent-ledger duplicates,
      kill-switch bypasses, approval-boundary bypasses, venue transmissions.

    Distinct from ACCEPTABLE non-zero counters (WS reconnects, quarantined
    partial lines, REST timeouts/429s, corrected reconciliation mismatches,
    book invalidations) which are evidence recovery worked — provided nothing
    remains unresolved.

    Returns a list of failing criterion names (empty => pass).
    """
    failures: list[str] = []
    checks = {
        "raw_hash_mismatches": report.hash_mismatches,
        "wire_fidelity_failures": report.wire_fidelity_failures,
        "manifest_chain_failures": report.manifest_chain_failures,
        "unresolved_book_mismatches": report.rest_unresolved_mismatches,
        "stale_delta_applications": report.stale_delta_applications,
        "replay_differences": (
            0 if report.replay_deterministic and report.replay_hash_a == report.replay_hash_b else 1
        ),
        "phase_machine_violations": report.phase_machine_violations,
        "intent_ledger_duplicates": report.intent_ledger_duplicates,
        "kill_switch_bypasses": report.kill_switch_bypasses,
        "approval_boundary_bypasses": report.approval_boundary_bypasses,
        "venue_transmissions_attempted": report.venue_transmissions_attempted,
    }
    for name, value in checks.items():
        if value != 0:
            failures.append(name)
    return failures


def gate_verdict(report: "BurnInReport") -> tuple[bool, list[str]]:
    """Full burn-in gate verdict from a BurnInReport.

    Returns (passed, failures) where failures combines zero-tolerance
    violations, replay non-determinism, and any recorded scenario FAIL.

    Hard qualifying requirements (a run must satisfy ALL to pass):
      research_eligible == True   (a --allow-dirty run can never qualify)
      working_tree_dirty == False
      baseline_commit == QUALIFYING_BASELINE_COMMIT
      research_logic_sha256 == QUALIFYING_RESEARCH_LOGIC_SHA256
      venue_transmissions_attempted == 0
      all zero-tolerance counters == 0
      replay_hash_a == replay_hash_b
      fault + live-ready checklist == PASS
    """
    failures = zero_tolerance_failures(report)
    if not report.research_eligible:
        failures.append("not_research_eligible")
    if report.working_tree_dirty:
        failures.append("working_tree_dirty")
    if report.baseline_commit != QUALIFYING_BASELINE_COMMIT:
        failures.append("baseline_commit_mismatch")
    if report.research_logic_sha256 != QUALIFYING_RESEARCH_LOGIC_SHA256:
        failures.append("research_logic_hash_mismatch")
    if not report.replay_deterministic or report.replay_hash_a != report.replay_hash_b:
        failures.append("replay_a_b_differ")
    if not report.metadata_reconstruction_ok:
        failures.append("metadata_reconstruction")
    if not report.resolution_lifecycle_ok:
        failures.append("resolution_lifecycle")
    if not report.crash_recovery_ok:
        failures.append("crash_recovery")
    for name, ok in (report.fault_injection or {}).items():
        if not ok:
            failures.append(f"fault:{name}")
    for name, ok in (report.live_ready or {}).items():
        if not ok:
            failures.append(f"live_ready:{name}")
    return (len(failures) == 0, failures)


# ── REAL_DATA_START marker ───────────────────────────────────────────────────


@dataclass(frozen=True)
class RealDataStartMarker:
    phase: str
    baseline_tag: str
    baseline_commit: str
    implementation_commit: str
    research_logic_sha256: str
    collector_sha256: str
    interface_sha256: str
    baseline_config_sha256: str
    feature_schema_sha256: str
    burnin_report_sha256: str
    started_at: str
    execution_mode: str
    alpha_status: str
    marker_sha256: str = ""

    def as_dict(self) -> dict:
        d = {
            "phase": self.phase,
            "baseline_tag": self.baseline_tag,
            "baseline_commit": self.baseline_commit,
            "implementation_commit": self.implementation_commit,
            "research_logic_sha256": self.research_logic_sha256,
            "collector_sha256": self.collector_sha256,
            "interface_sha256": self.interface_sha256,
            "baseline_config_sha256": self.baseline_config_sha256,
            "feature_schema_sha256": self.feature_schema_sha256,
            "burnin_report_sha256": self.burnin_report_sha256,
            "started_at": self.started_at,
            "execution_mode": self.execution_mode,
            "alpha_status": self.alpha_status,
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
    implementation_commit: str,
    research_logic_sha256: str,
    collector_sha256: str,
    interface_sha256: str,
    baseline_config_sha256: str,
    feature_schema_sha256: str,
    burnin_report_sha256: str,
    started_at: str | None = None,
    execution_mode: str = "SHADOW_OR_COLLECTION_ONLY",
    alpha_status: str = "UNKNOWN",
    phase_store: "PhaseStore | None" = None,
) -> RealDataStartMarker:
    """Write the immutable REAL_DATA_START marker. Refuses to overwrite.

    Permanently binds the experiment to the exact state at commencement:
    the frozen research baseline (baseline_commit, research_logic_sha256) and
    the hardened operational code running at the start (implementation_commit,
    collector_sha256, interface_sha256).

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
        baseline_tag=baseline_tag,
        baseline_commit=baseline_commit,
        implementation_commit=implementation_commit,
        research_logic_sha256=research_logic_sha256,
        collector_sha256=collector_sha256,
        interface_sha256=interface_sha256,
        baseline_config_sha256=baseline_config_sha256,
        feature_schema_sha256=feature_schema_sha256,
        burnin_report_sha256=burnin_report_sha256,
        started_at=started_at or datetime.now(UTC).isoformat(),
        execution_mode=execution_mode,
        alpha_status=alpha_status,
    )
    digest = marker.combined_hash()
    marker = RealDataStartMarker(
        phase=marker.phase,
        baseline_tag=marker.baseline_tag,
        baseline_commit=marker.baseline_commit,
        implementation_commit=marker.implementation_commit,
        research_logic_sha256=marker.research_logic_sha256,
        collector_sha256=marker.collector_sha256,
        interface_sha256=marker.interface_sha256,
        baseline_config_sha256=marker.baseline_config_sha256,
        feature_schema_sha256=marker.feature_schema_sha256,
        burnin_report_sha256=marker.burnin_report_sha256,
        started_at=marker.started_at,
        execution_mode=marker.execution_mode,
        alpha_status=marker.alpha_status,
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
    implementation_commit: str
    research_logic_sha256: str
    collector_sha256: str
    interface_sha256: str

    # Collection
    messages: int
    markets: int
    tokens_observed: int
    lifecycle_events: int
    resolved_markets: int
    reconnects: int
    forced_failures: int

    # Raw integrity
    hash_mismatches: int
    unexplained_partial_lines: int
    manifest_chain_failures: int
    wire_fidelity_failures: int

    # Research eligibility: a dirty-worktree (--allow-dirty) run must NEVER be
    # able to qualify for FINAL GATE: PASS.
    research_eligible: bool = True
    working_tree_dirty: bool = False

    # Replay
    replay_hash_a: str = ""
    replay_hash_b: str = ""
    replay_deterministic: bool = False

    # Book fidelity
    rest_reconciliations: int = 0
    rest_matching: int = 0
    rest_corrected_mismatches: int = 0
    rest_unresolved_mismatches: int = 0
    stale_delta_applications: int = 0

    # Clocks (ms)
    receive_lag_p50: float | None = None
    receive_lag_p95: float | None = None
    receive_lag_p99: float | None = None
    processing_lag_p50: float | None = None
    processing_lag_p95: float | None = None
    processing_lag_p99: float | None = None
    clock_anomalies: int = 0

    # Legacy fields kept for compatibility
    raw_corruption: int = 0
    partial_records: int = 0
    invalid_delta_applications: int = 0
    unresolved_book_mismatches: int = 0
    manifest_chain_ok: bool = True
    metadata_reconstruction_ok: bool = True
    resolution_lifecycle_ok: bool = True
    crash_recovery_ok: bool = True
    gate_passed: bool = False

    # Live-ready scoreboard
    phase_machine_violations: int = 0
    intent_ledger_duplicates: int = 0
    kill_switch_bypasses: int = 0
    approval_boundary_bypasses: int = 0
    venue_transmissions_attempted: int = 0

    # Fault-injection results (scenario -> PASS/FAIL)
    fault_injection: dict[str, bool] = field(default_factory=dict)

    # Live-ready scenario results (scenario -> PASS/FAIL)
    live_ready: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "baseline_commit": self.baseline_commit,
            "implementation_commit": self.implementation_commit,
            "research_logic_sha256": self.research_logic_sha256,
            "collector_sha256": self.collector_sha256,
            "interface_sha256": self.interface_sha256,
            "research_eligible": self.research_eligible,
            "working_tree_dirty": self.working_tree_dirty,
            "messages": self.messages,
            "markets": self.markets,
            "tokens_observed": self.tokens_observed,
            "lifecycle_events": self.lifecycle_events,
            "resolved_markets": self.resolved_markets,
            "reconnects": self.reconnects,
            "forced_failures": self.forced_failures,
            "hash_mismatches": self.hash_mismatches,
            "unexplained_partial_lines": self.unexplained_partial_lines,
            "manifest_chain_failures": self.manifest_chain_failures,
            "wire_fidelity_failures": self.wire_fidelity_failures,
            "replay_hash_a": self.replay_hash_a,
            "replay_hash_b": self.replay_hash_b,
            "replay_deterministic": self.replay_deterministic,
            "rest_reconciliations": self.rest_reconciliations,
            "rest_matching": self.rest_matching,
            "rest_corrected_mismatches": self.rest_corrected_mismatches,
            "rest_unresolved_mismatches": self.rest_unresolved_mismatches,
            "stale_delta_applications": self.stale_delta_applications,
            "receive_lag_p50": self.receive_lag_p50,
            "receive_lag_p95": self.receive_lag_p95,
            "receive_lag_p99": self.receive_lag_p99,
            "processing_lag_p50": self.processing_lag_p50,
            "processing_lag_p95": self.processing_lag_p95,
            "processing_lag_p99": self.processing_lag_p99,
            "clock_anomalies": self.clock_anomalies,
            "metadata_reconstruction_ok": self.metadata_reconstruction_ok,
            "resolution_lifecycle_ok": self.resolution_lifecycle_ok,
            "crash_recovery_ok": self.crash_recovery_ok,
            "gate_passed": self.gate_passed,
            "phase_machine_violations": self.phase_machine_violations,
            "intent_ledger_duplicates": self.intent_ledger_duplicates,
            "kill_switch_bypasses": self.kill_switch_bypasses,
            "approval_boundary_bypasses": self.approval_boundary_bypasses,
            "venue_transmissions_attempted": self.venue_transmissions_attempted,
            "fault_injection": self.fault_injection,
            "live_ready": self.live_ready,
        }
        return d

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


# ── Per-fault evidence (so "22/22 PASS" has a record of what the 22 were) ──


@dataclass(frozen=True)
class FaultInjectionEvent:
    """One injected fault and its observed recovery behavior.

    Matches the review's schema exactly: when the report says
    "Fault injection: 22/22 PASS" there is a machine-readable record of what
    those 22 tests actually were, without needing to read code.
    """

    fault_id: str
    type: str
    started_at: str
    expected_behavior: list[str]
    observed_behavior: str
    result: str  # "PASS" | "FAIL"

    def as_dict(self) -> dict:
        return {
            "fault_id": self.fault_id,
            "type": self.type,
            "started_at": self.started_at,
            "expected_behavior": self.expected_behavior,
            "observed_behavior": self.observed_behavior,
            "result": self.result,
        }


class FaultEvidenceWriter:
    """Append-only JSONL of injected-fault events, one per fault.

    Written under the burn-in report directory as fault-evidence.jsonl so the
    integrity report and its evidence are co-located and auditable.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def record(self, event: FaultInjectionEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.as_dict(), sort_keys=True) + "\n")

    def read_all(self) -> list[FaultInjectionEvent]:
        if not self.path.exists():
            return []
        events: list[FaultInjectionEvent] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue  # partial trailing line from a crash
                events.append(
                    FaultInjectionEvent(
                        fault_id=data["fault_id"],
                        type=data["type"],
                        started_at=data["started_at"],
                        expected_behavior=data["expected_behavior"],
                        observed_behavior=data["observed_behavior"],
                        result=data["result"],
                    )
                )
        return events

    def summary(self) -> dict:
        events = self.read_all()
        return {
            "total": len(events),
            "passed": sum(1 for e in events if e.result == "PASS"),
            "failed": sum(1 for e in events if e.result == "FAIL"),
        }


def _render_burnin_markdown(report: BurnInReport) -> str:
    def check(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    def pct(part: int, total: int) -> str:
        return f"{part/total*100:.2f}%" if total else "n/a"

    fi = report.fault_injection or {}
    lr = report.live_ready or {}
    fi_ok = all(fi.values()) if fi else True
    lr_ok = all(lr.values()) if lr else True

    lines = [
        "POLYALPHA v0.3.0",
        "BURN-IN INTEGRITY REPORT",
        "",
        "BASELINE",
        "Immutable tag: v0.3.0-research-baseline-334b911",
        f"Baseline commit: {report.baseline_commit}",
        f"Implementation commit: {report.implementation_commit}",
        f"Research logic SHA256: {report.research_logic_sha256}",
        f"Collector SHA256: {report.collector_sha256}",
        f"Interface SHA256: {report.interface_sha256}",
        f"Worktree at launch: {'CLEAN' if not report.working_tree_dirty else 'DIRTY (NOT ELIGIBLE)'}",
        f"Research eligible: {'YES' if report.research_eligible else 'NO'}",
        "",
        "COLLECTION",
        f"Duration: {report.period_start} → {report.period_end}",
        f"Raw messages: {report.messages:,}",
        f"Markets observed: {report.markets}",
        f"Tokens observed: {report.tokens_observed}",
        f"Lifecycle events: {report.lifecycle_events}",
        f"Resolved markets: {report.resolved_markets}",
        f"Reconnects: {report.reconnects}",
        f"Forced failures: {report.forced_failures}",
        "",
        "RAW INTEGRITY",
        f"Hash mismatches: {report.hash_mismatches}",
        f"Unexplained partial lines: {report.unexplained_partial_lines}",
        f"Manifest-chain failures: {report.manifest_chain_failures}",
        f"Wire fidelity failures: {report.wire_fidelity_failures}",
        "",
        "REPLAY",
        f"Replay A hash: {report.replay_hash_a}",
        f"Replay B hash: {report.replay_hash_b}",
        f"Deterministic: {check(report.replay_deterministic)}",
        "",
        "BOOK FIDELITY",
        f"REST reconciliations: {report.rest_reconciliations}",
        f"Matching: {report.rest_matching} ({pct(report.rest_matching, report.rest_reconciliations)})",
        f"Corrected mismatches: {report.rest_corrected_mismatches}",
        f"Unresolved mismatches: {report.rest_unresolved_mismatches}",
        f"Stale-delta applications: {report.stale_delta_applications}",
        "",
        "CLOCKS",
        f"Exchange→receive P50: {report.receive_lag_p50} ms",
        f"Exchange→receive P95: {report.receive_lag_p95} ms",
        f"Exchange→receive P99: {report.receive_lag_p99} ms",
        f"Processing P50: {report.processing_lag_p50} ms",
        f"Processing P95: {report.processing_lag_p95} ms",
        f"Processing P99: {report.processing_lag_p99} ms",
        f"Clock anomalies: {report.clock_anomalies} unexplained",
        "",
        "FAULT INJECTION",
    ]
    if fi:
        lines += [f"{name}: {check(ok)}" for name, ok in sorted(fi.items())]
    else:
        lines.append("(no scenarios recorded)")
    lines += [
        "",
        "LIVE-READY SAFETY",
        f"Phase-machine violations: {report.phase_machine_violations}",
        f"Intent-ledger duplicates: {report.intent_ledger_duplicates}",
        f"Kill-switch bypasses: {report.kill_switch_bypasses}",
        f"Approval-boundary bypasses: {report.approval_boundary_bypasses}",
        f"Venue transmissions attempted: {report.venue_transmissions_attempted}",
    ]
    if lr:
        lines += [f"{name}: {check(ok)}" for name, ok in sorted(lr.items())]
    lines += [
        "",
        "FINAL GATE",
        f"Gate: {check(report.gate_passed and fi_ok and lr_ok)}",
        "",
    ]
    return "\n".join(lines)