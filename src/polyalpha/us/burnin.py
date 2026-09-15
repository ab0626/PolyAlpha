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
from dataclasses import dataclass
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
    """US implementation commit + hashes + phase + worktree at burn-in start."""

    implementation_commit: str
    collector_sha256: str
    interface_sha256: str
    instrument_snapshot_hash: str
    reconciliation_policy_sha256: str
    phase: str
    working_tree_dirty: bool

    def as_dict(self) -> dict:
        return {
            "implementation_commit": self.implementation_commit,
            "collector_sha256": self.collector_sha256,
            "interface_sha256": self.interface_sha256,
            "instrument_snapshot_hash": self.instrument_snapshot_hash,
            "reconciliation_policy_sha256": self.reconciliation_policy_sha256,
            "phase": self.phase,
            "working_tree_dirty": self.working_tree_dirty,
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
        """US FINAL GATE: zero-tolerance hard failures + replay + faults."""
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
        return (len(failures) == 0, failures)

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
        f"Instrument snapshot hash: {report.provenance.instrument_snapshot_hash}",
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


def instrument_snapshot_hash(instruments) -> str:
    """Deterministic hash of the authoritative instrument snapshot.

    Locks the exact instrument-normalization rules (symbol, tickSize,
    minimumTradeQty, priceScale, state) that the burn-in validates against.
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
                    "state": inst.state.value,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return h.hexdigest()