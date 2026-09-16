"""US burn-in hardening — health, gates, and provenance for the US lineage.

Part of the US adapter family. Mirrors the International burn-in philosophy but
with US semantics explicitly represented. The US burn-in report surfaces
US-specific failure modes rather than reusing International counters verbatim.

Qualifying path:
    US_BASELINE_FROZEN
    → US_BURNIN_RUNNING
    → live US collection
    → fault injection
    → reconciliation
    → A/B replay
    → US FINAL GATE: PASS
    → US_BURNIN_PASSED
    → REAL_DATA_START_US
    → US_COLLECTION_RUNNING

The US baseline must be frozen BEFORE the qualifying burn-in, locking the actual
US collector/interface configuration (instrument-normalization rules and
reconciliation policy) so the burn-in doesn't prove a moving target.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..replay_verification import ReplayCheckResult
from .grpc_stream import StreamEvent
from .reconcile import ReconciliationReason, ReconciliationResult


@dataclass
class UsHealth:
    """US-specific health counters collected during burn-in."""

    grpc_heartbeats: int = 0
    grpc_reconnects: int = 0
    grpc_out_of_order_updates: int = 0
    grpc_stale_invalidations: int = 0
    subscription_errors: int = 0
    unknown_symbol_events: int = 0
    rest_429s: int = 0
    rest_reconciliations: int = 0
    retail_secondary_mismatches: int = 0
    price_scale_mismatches: int = 0
    tick_size_mismatches: int = 0
    state_mismatches: int = 0
    unresolved_book_mismatches: int = 0
    stale_state_applications: int = 0

    def as_dict(self) -> dict:
        return {
            "grpc_heartbeats": self.grpc_heartbeats,
            "grpc_reconnects": self.grpc_reconnects,
            "grpc_out_of_order_updates": self.grpc_out_of_order_updates,
            "grpc_stale_invalidations": self.grpc_stale_invalidations,
            "subscription_errors": self.subscription_errors,
            "unknown_symbol_events": self.unknown_symbol_events,
            "rest_429s": self.rest_429s,
            "rest_reconciliations": self.rest_reconciliations,
            "retail_secondary_mismatches": self.retail_secondary_mismatches,
            "price_scale_mismatches": self.price_scale_mismatches,
            "tick_size_mismatches": self.tick_size_mismatches,
            "state_mismatches": self.state_mismatches,
            "unresolved_book_mismatches": self.unresolved_book_mismatches,
            "stale_state_applications": self.stale_state_applications,
        }


class UsHealthTracker:
    """Consumes stream events and reconciliation results into US health."""

    def __init__(self) -> None:
        self.health = UsHealth()

    def on_stream_event(self, event: StreamEvent) -> None:
        if event.kind == "heartbeat":
            self.health.grpc_heartbeats += 1
        elif event.kind == "reconnect":
            self.health.grpc_reconnects += 1
        elif event.kind == "stale":
            if "out-of-order" in event.message:
                self.health.grpc_out_of_order_updates += 1
            else:
                self.health.grpc_stale_invalidations += 1
        elif event.kind == "error":
            self.health.subscription_errors += 1
            if "identifier" in event.message or "priceScale" in event.message:
                self.health.unknown_symbol_events += 1

    def on_reconciliation(self, result: ReconciliationResult) -> None:
        self.health.rest_reconciliations += 1
        reason = result.reason
        if reason == ReconciliationReason.PRICE_SCALE_MISMATCH:
            self.health.price_scale_mismatches += 1
        elif reason == ReconciliationReason.STATE_MISMATCH:
            self.health.state_mismatches += 1
        elif reason == ReconciliationReason.LEVEL_MISMATCH:
            self.health.unresolved_book_mismatches += 1
        elif reason == ReconciliationReason.UNRESOLVED:
            self.health.unresolved_book_mismatches += 1
        elif reason == ReconciliationReason.RETAIL_LAG:
            self.health.retail_secondary_mismatches += 1

    def on_rest_429(self) -> None:
        self.health.rest_429s += 1

    def on_stale_state_application(self) -> None:
        self.health.stale_state_applications += 1


@dataclass
class UsBurnInProvenance:
    """US implementation commit + policy/version hashes + phase + worktree.

    Three-layer model:
      FROZEN POLICY      normalization_policy_sha256, reconciliation_policy_sha256,
                         instrument_policy_sha256 (effectively-immutable fields only)
      LAUNCH SNAPSHOT    instrument_policy_sha256_at_start (anchor)
      DURING RUN         versioned instrument metadata updates (append-only,
                         timestamped, hashed) — state evolution is observed,
                         NOT a failure
    """

    implementation_commit: str
    collector_sha256: str
    interface_sha256: str
    instrument_policy_sha256: str  # immutable-field anchor (symbol/tick/minQty/priceScale)
    reconciliation_policy_sha256: str
    phase: str
    working_tree_dirty: bool
    metadata_chain: "UsInstrumentMetadataChain | None" = None

    def __post_init__(self) -> None:
        if self.metadata_chain is None:
            object.__setattr__(self, "metadata_chain", UsInstrumentMetadataChain())

    @property
    def chain(self) -> "UsInstrumentMetadataChain":
        return self.metadata_chain

    def as_dict(self) -> dict:
        return {
            "implementation_commit": self.implementation_commit,
            "collector_sha256": self.collector_sha256,
            "interface_sha256": self.interface_sha256,
            "instrument_policy_sha256": self.instrument_policy_sha256,
            "reconciliation_policy_sha256": self.reconciliation_policy_sha256,
            "phase": self.phase,
            "working_tree_dirty": self.working_tree_dirty,
            "instrument_metadata_chain": self.metadata_chain.as_dict(),
        }


@dataclass
class UsBurnInReport:
    period_start: str
    period_end: str
    baseline: str  # US baseline tag (e.g. v0.4.0-us-research-baseline-<commit>)
    provenance: UsBurnInProvenance
    health: UsHealth
    replay: ReplayCheckResult
    fault_injection_passed: bool
    gate_passed: bool = False
    # Final observed instrument-policy hash (computed from the live instrument
    # set at report time). If it differs from the launch anchor with no
    # attributed metadata-chain entry, that is unexplained policy drift.
    final_instrument_policy_sha256: str = ""
    # Fields of the frozen instrument policy that the venue contract EXPLICITLY
    # permits to change during a run (e.g. none for priceScale/tickSize/minQty
    # by default). A recorded change to a field NOT listed here is attributable
    # but policy-breaking -> NON-QUALIFYING.
    permitted_policy_changes: frozenset = frozenset()

    def hard_failure_count(self) -> int:
        """US-specific zero-tolerance hard-failure counters."""
        h = self.health
        return sum(
            [
                h.unknown_symbol_events,
                h.price_scale_mismatches,
                h.tick_size_mismatches,
                h.state_mismatches,
                h.grpc_out_of_order_updates,
                h.unresolved_book_mismatches,
                h.stale_state_applications,
            ]
        )

    def qualifying(self) -> tuple[bool, list[str]]:
        """US FINAL GATE: zero-tolerance hard failures + replay + faults.

        Three-way instrument provenance semantics:
          1. expected mutable metadata change (state lifecycle) recorded in the
             chain                  -> PASS
          2. frozen-policy change, unrecorded
                                     -> HARD FAIL (instrument_policy_drift)
          3. frozen-policy change, recorded but NOT in permitted_policy_changes
                                     -> NON-QUALIFYING
                                     (instrument_policy_change_not_permitted)
             Frozen-policy change, recorded AND permitted -> PASS
        """
        failures: list[str] = []
        h = self.health
        if h.unknown_symbol_events:
            failures.append(f"unknown_symbol_events={h.unknown_symbol_events}")
        if h.price_scale_mismatches:
            failures.append(f"price_scale_mismatches={h.price_scale_mismatches}")
        if h.tick_size_mismatches:
            failures.append(f"tick_size_mismatches={h.tick_size_mismatches}")
        if h.state_mismatches:
            failures.append(f"state_mismatches={h.state_mismatches}")
        if h.grpc_out_of_order_updates:
            failures.append(f"grpc_out_of_order_updates={h.grpc_out_of_order_updates}")
        if h.unresolved_book_mismatches:
            failures.append(f"unresolved_book_mismatches={h.unresolved_book_mismatches}")
        if h.stale_state_applications:
            failures.append(f"stale_state_applications={h.stale_state_applications}")
        if not self.replay.deterministic or self.replay.hash_a != self.replay.hash_b:
            failures.append("replay_a_b_differ")
        if not self.fault_injection_passed:
            failures.append("fault_injection")
        if self.provenance.working_tree_dirty:
            failures.append("working_tree_dirty")
        # Instrument policy provenance: three-way distinction.
        policy_status = self._policy_status
        if policy_status == "hard_failure":
            failures.append("instrument_policy_drift")
        elif policy_status == "not_permitted":
            failures.append("instrument_policy_change_not_permitted")
        return (len(failures) == 0, failures)

    @property
    def _policy_status(self) -> str:
        """Classify the instrument policy anchor vs the metadata chain.

        Returns one of:
          "ok"             policy unchanged, or change recorded AND permitted
          "hard_failure"   policy changed, change NOT recorded (unexplained)
          "not_permitted"  policy changed, change recorded but NOT permitted
        """
        if not self.final_instrument_policy_sha256:
            return "ok"  # no final hash supplied -> cannot assert change
        if self.final_instrument_policy_sha256 == self.provenance.instrument_policy_sha256:
            return "ok"  # policy unchanged

        # A frozen-policy field changed (policy hash differs). Find which fields
        # were recorded as changed in the metadata chain.
        policy_fields = ("tick_size", "price_scale", "minimum_trade_qty")
        recorded_policy_changes = [
            u for u in self.provenance.metadata_chain.updates
            if u["field"] in policy_fields
        ]
        if not recorded_policy_changes:
            # Policy changed but nothing was recorded -> unexplained drift.
            return "hard_failure"

        # Policy changed and is recorded. It is only acceptable if the field is
        # explicitly permitted by the venue contract.
        permitted = self.permitted_policy_changes
        recorded_fields = {u["field"] for u in recorded_policy_changes}
        if recorded_fields.issubset(permitted):
            return "ok"  # recorded AND explicitly permitted
        return "not_permitted"

    def as_dict(self) -> dict:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "baseline": self.baseline,
            "provenance": self.provenance.as_dict(),
            "health": self.health.as_dict(),
            "replay": self.replay.as_dict(),
            "fault_injection_passed": self.fault_injection_passed,
            "hard_failure_count": self.hard_failure_count(),
            "final_instrument_policy_sha256": self.final_instrument_policy_sha256,
            "permitted_policy_changes": sorted(self.permitted_policy_changes),
            "instrument_policy_status": self._policy_status,
            "gate_passed": self.gate_passed,
        }

    def combined_hash(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_us_burnin_markdown(report: UsBurnInReport) -> str:
    def check(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    h = report.health
    lines = [
        "POLYALPHA US — BURN-IN INTEGRITY REPORT",
        "",
        "BASELINE",
        f"US baseline: {report.baseline}",
        f"Implementation commit: {report.provenance.implementation_commit}",
        f"Collector SHA256: {report.provenance.collector_sha256}",
        f"Interface SHA256: {report.provenance.interface_sha256}",
        f"Instrument policy SHA256 (launch anchor): {report.provenance.instrument_policy_sha256}",
        f"Instrument policy SHA256 (final observed): {report.final_instrument_policy_sha256 or '(not supplied)'}",
        f"Instrument policy status: {report._policy_status}",
        f"Instrument policy permitted changes: {', '.join(sorted(report.permitted_policy_changes)) or '(none)'}",
        f"Instrument metadata updates: {report.provenance.metadata_chain.as_dict()['update_count']}",
        f"Instrument metadata chain root: {report.provenance.metadata_chain.root_hash()}",
        f"Reconciliation policy SHA256: {report.provenance.reconciliation_policy_sha256}",
        f"Phase: {report.provenance.phase}",
        f"Worktree at launch: {'CLEAN' if not report.provenance.working_tree_dirty else 'DIRTY (NOT ELIGIBLE)'}",
        "",
        "PERIOD",
        f"Duration: {report.period_start} → {report.period_end}",
        "",
        "US HEALTH",
        f"gRPC heartbeats: {h.grpc_heartbeats}",
        f"gRPC reconnects: {h.grpc_reconnects}",
        f"gRPC out-of-order updates: {h.grpc_out_of_order_updates}",
        f"gRPC stale invalidations: {h.grpc_stale_invalidations}",
        f"Subscription errors: {h.subscription_errors}",
        f"REST 429s: {h.rest_429s}",
        f"REST reconciliations: {h.rest_reconciliations}",
        "",
        "US ZERO-TOLERANCE",
        f"Unknown symbol events: {h.unknown_symbol_events}",
        f"Price-scale mismatches: {h.price_scale_mismatches}",
        f"Tick-size mismatches: {h.tick_size_mismatches}",
        f"State mismatches: {h.state_mismatches}",
        f"Unresolved book mismatches: {h.unresolved_book_mismatches}",
        f"Retail secondary mismatches: {h.retail_secondary_mismatches}",
        f"Stale-state applications: {h.stale_state_applications}",
        "",
        "REPLAY",
        f"Replay A hash: {report.replay.hash_a}",
        f"Replay B hash: {report.replay.hash_b}",
        f"A == B: {check(report.replay.deterministic and report.replay.hash_a == report.replay.hash_b)}",
        "",
        "FAULT INJECTION",
        f"Fault injection: {check(report.fault_injection_passed)}",
        "",
        "US FINAL GATE",
        f"Gate: {check(report.gate_passed)}",
        "",
    ]
    return "\n".join(lines)


def write_us_burnin_report(
    report: UsBurnInReport, directory: str | Path
) -> tuple[Path, str]:
    """Write burnin-report.{json,md} for the US lineage; returns (dir, sha256)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    digest = report.combined_hash()

    json_path = directory / "us-burnin-report.json"
    json_path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / "us-burnin-report.md").write_text(
        render_us_burnin_markdown(report), encoding="utf-8"
    )
    return directory, digest


def instrument_policy_hash(instruments) -> str:
    """Deterministic hash of the EFFECTIVELY-IMMUTABLE instrument fields.

    Locks the exact instrument-normalization rules that the burn-in validates
    against: symbol, tickSize, minimumTradeQty, priceScale. These must NOT
    change silently during a run; drift here is a hard failure.

    Deliberately EXCLUDES state (and any other field that legitimately evolves,
    e.g. PREOPEN -> OPEN -> CLOSED). State evolution is captured by the
    versioned metadata chain, not the policy anchor.
    """
    h = hashlib.sha256()
    for symbol in sorted(instruments._by_symbol):
        inst = instruments._by_symbol[symbol]
        h.update(
            json.dumps(
                {
                    "symbol": inst.symbol,
                    "tick_size": str(inst.tick_size),
                    "minimum_trade_qty": str(inst.minimum_trade_qty),
                    "price_scale": inst.price_scale,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return h.hexdigest()


@dataclass
class UsInstrumentMetadataChain:
    """Versioned, append-only, timestamped, hashed instrument metadata updates.

    Fields that legitimately evolve during a run (e.g. market state) are
    recorded here as observed + attributed + replayable versions — they are
    NOT provenance failures. The chain root hash proves the full update
    history was captured deterministically.
    """

    updates: list[dict] = field(default_factory=list)

    def record(self, symbol: str, field: str, old_value, new_value) -> dict:
        from datetime import timezone

        update = {
            "sequence": len(self.updates) + 1,
            "symbol": symbol,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # Chain: each update is hashed together with the prior root, making
        # the history append-only and tamper-evident.
        prior = self.root_hash()
        update["prior_hash"] = prior
        canonical = json.dumps(update, sort_keys=True, separators=(",", ":"))
        update["hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.updates.append(update)
        return update

    def root_hash(self) -> str:
        """Merkle-ish root over the full metadata history."""
        if not self.updates:
            return hashlib.sha256(b"").hexdigest()
        return self.updates[-1]["hash"]

    def as_dict(self) -> dict:
        return {
            "update_count": len(self.updates),
            "root_hash": self.root_hash(),
            "updates": list(self.updates),
        }


@dataclass
class UsRealDataStartMarker:
    """Immutable REAL_DATA_START_US marker — binds the US experiment at start.

    Mirrors the International RealDataStartMarker discipline for the US
    lineage. Permanently binds:
        baseline identity, implementation_commit, collector/interface SHA256,
        instrument policy SHA256, reconciliation policy SHA256,
        burn-in report SHA256, burn-in replay hash, started_at (UTC),
        research state (ALPHA_UNKNOWN while the model is locked).
    The marker_sha256 self-hash proves the binding was not edited after write.
    """

    phase: str
    baseline_tag: str
    baseline_commit: str
    implementation_commit: str
    collector_sha256: str
    interface_sha256: str
    instrument_policy_sha256: str
    reconciliation_policy_sha256: str
    burnin_report_sha256: str
    burnin_replay_hash_a: str
    burnin_replay_hash_b: str
    started_at: str
    research_state: str
    execution_mode: str
    marker_writer_commit: str = ""
    marker_sha256: str = ""

    def as_dict(self) -> dict:
        d = {
            "phase": self.phase,
            "baseline_tag": self.baseline_tag,
            "baseline_commit": self.baseline_commit,
            "implementation_commit": self.implementation_commit,
            "collector_sha256": self.collector_sha256,
            "interface_sha256": self.interface_sha256,
            "instrument_policy_sha256": self.instrument_policy_sha256,
            "reconciliation_policy_sha256": self.reconciliation_policy_sha256,
            "burnin_report_sha256": self.burnin_report_sha256,
            "burnin_replay_hash_a": self.burnin_replay_hash_a,
            "burnin_replay_hash_b": self.burnin_replay_hash_b,
            "started_at": self.started_at,
            "research_state": self.research_state,
            "execution_mode": self.execution_mode,
        }
        if self.marker_writer_commit:
            d["marker_writer_commit"] = self.marker_writer_commit
        if self.marker_sha256:
            d["marker_sha256"] = self.marker_sha256
        return d

    def combined_hash(self) -> str:
        d = self.as_dict()
        d.pop("marker_sha256", None)
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_us_real_data_start_marker(
    marker_path: str | Path,
    baseline_tag: str,
    baseline_commit: str,
    implementation_commit: str,
    collector_sha256: str,
    interface_sha256: str,
    instrument_policy_sha256: str,
    reconciliation_policy_sha256: str,
    burnin_report_sha256: str,
    burnin_replay_hash_a: str,
    burnin_replay_hash_b: str,
    started_at: str | None = None,
    research_state: str = "ALPHA_UNKNOWN",
    execution_mode: str = "SHADOW_OR_COLLECTION_ONLY",
    marker_writer_commit: str = "",
    phase_store: "object | None" = None,
) -> UsRealDataStartMarker:
    """Write the immutable REAL_DATA_START_US marker. Refuses to overwrite.

    Permanently binds the US experiment to the exact state at commencement:
    the frozen US baseline, the hardened operational code at start
    (implementation_commit, collector_sha256, interface_sha256), the US
    instrument + reconciliation policy hashes, and the qualifying burn-in
    report. When a phase_store is provided, it must be in US_BURNIN_PASSED
    phase; otherwise the marker write is rejected.
    """
    if phase_store is not None:
        phase_store.require("US_BURNIN_PASSED")
    path = Path(marker_path)
    if path.exists():
        raise FileExistsError(f"REAL_DATA_START_US marker already exists: {path}")
    marker = UsRealDataStartMarker(
        phase="REAL_DATA_START_US",
        baseline_tag=baseline_tag,
        baseline_commit=baseline_commit,
        implementation_commit=implementation_commit,
        collector_sha256=collector_sha256,
        interface_sha256=interface_sha256,
        instrument_policy_sha256=instrument_policy_sha256,
        reconciliation_policy_sha256=reconciliation_policy_sha256,
        burnin_report_sha256=burnin_report_sha256,
        burnin_replay_hash_a=burnin_replay_hash_a,
        burnin_replay_hash_b=burnin_replay_hash_b,
        started_at=started_at or datetime.now(UTC).isoformat(),
        research_state=research_state,
        execution_mode=execution_mode,
        marker_writer_commit=marker_writer_commit,
    )
    digest = marker.combined_hash()
    marker = UsRealDataStartMarker(
        phase=marker.phase,
        baseline_tag=marker.baseline_tag,
        baseline_commit=marker.baseline_commit,
        implementation_commit=marker.implementation_commit,
        collector_sha256=marker.collector_sha256,
        interface_sha256=marker.interface_sha256,
        instrument_policy_sha256=marker.instrument_policy_sha256,
        reconciliation_policy_sha256=marker.reconciliation_policy_sha256,
        burnin_report_sha256=marker.burnin_report_sha256,
        burnin_replay_hash_a=marker.burnin_replay_hash_a,
        burnin_replay_hash_b=marker.burnin_replay_hash_b,
        started_at=marker.started_at,
        research_state=marker.research_state,
        execution_mode=marker.execution_mode,
        marker_writer_commit=marker.marker_writer_commit,
        marker_sha256=digest,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(marker.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return marker