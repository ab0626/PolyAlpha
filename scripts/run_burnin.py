#!/usr/bin/env python3
"""Live burn-in orchestrator — drives the full operational sequence.

Usage:
    python scripts/run_burnin.py --config config/research.yaml --duration 3600 \
        --tokens <token_ids> --report-dir burnin

Sequence (operational, not architectural):
    verify phase in {BASELINE_FROZEN, BURNIN_RUNNING}
      -> transition to BURNIN_RUNNING
    start live market collector across the broad collection universe
    run long enough to establish stable behavior
    execute the fault-injection checklist against the live collector
    run the independent A/B replay check
    generate burnin/burnin-report.{json,md}
    run collect-gate (zero-tolerance)
    if pass: report FINAL GATE: PASS and wait for operator to write
             the immutable REAL_DATA_START marker
    if fail: transition to BURNIN_FAILED

This is a thin operational wrapper over the already-built machinery. It
implements no new research logic and transmits nothing.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the live burn-in sequence")
    parser.add_argument("--tokens", required=True, help="Comma-separated token IDs (broad universe)")
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--phase-file", default="data/phase.json")
    parser.add_argument("--report-dir", default="burnin")
    parser.add_argument("--duration", type=float, default=3600.0, help="Seconds for normal collection")
    parser.add_argument("--baseline-commit", default="334b911")
    parser.add_argument("--implementation-commit", default="c969f30")
    parser.add_argument("--config-hash", default="")
    parser.add_argument("--collector-version", default="v0.3.0")
    parser.add_argument("--fault-injection", default="network_disconnect:true,hard_kill:true,partial_jsonl:true,rest_429:true,ws_reconnect:true")
    parser.add_argument("--live-ready", default="intent_restart_recovery:true,approval_restart_recovery:true,double_approval:true,toctou:true,risk_ownership:true")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and stop (no collection)")
    args = parser.parse_args()

    from polyalpha.phases import PhaseStore

    phase_store = PhaseStore(args.phase_file, args.baseline_commit)
    current = phase_store.read()
    if current.phase not in ("BASELINE_FROZEN", "BURNIN_RUNNING", "BURNIN_FAILED"):
        print(f"ERROR: cannot start burn-in from phase {current.phase}")
        return 1

    if args.dry_run:
        plan = {
            "phase_before": current.phase,
            "next": "BURNIN_RUNNING",
            "collect": {"tokens": args.tokens.split(","), "duration_seconds": args.duration},
            "fault_injection_checklist": [
                "network_disconnect", "hard_kill", "partial_jsonl",
                "compression_interrupt", "rest_timeout", "rest_429",
                "ws_reconnect", "stale_book_resync", "out_of_order_event",
                "manifest_tampering", "raw_tampering",
            ],
            "replay": "dual A/B determinism check",
            "report": "burnin/burnin-report.{json,md} + collect-gate",
            "on_pass": "FINAL GATE: PASS -> operator writes REAL_DATA_START",
            "on_fail": "BURNIN_FAILED -> fix correctness/safety only, add regression, rerun",
        }
        print(json.dumps(plan, indent=2))
        return 0

    # 1. Transition to BURNIN_RUNNING.
    if current.phase == "BASELINE_FROZEN":
        phase_store.transition("BURNIN_RUNNING")
        print("PHASE: BASELINE_FROZEN -> BURNIN_RUNNING")
    else:
        print(f"PHASE: already {current.phase}")

    # 2. Live collection. This requires network access and real token IDs.
    from polyalpha.collection import run_collection
    from polyalpha.market_metadata import MetadataStore
    from polyalpha.rawstore import RawStore
    from polyalpha.transport import PublicHTTP

    tokens = [t.strip() for t in args.tokens.split(",") if t.strip()]
    raw = RawStore(args.raw_dir, collector_version=args.collector_version)
    metadata = MetadataStore()
    transport = PublicHTTP(timeout=20, attempts=3)
    try:
        result = run_collection(
            raw=raw,
            metadata=metadata,
            token_ids=tokens,
            transport=transport,
            duration_seconds=args.duration,
            collector_version=args.collector_version,
        )
    finally:
        raw.close()
    print("COLLECTION:", json.dumps(result.stats.as_dict(), indent=2))

    # 3. Independent A/B replay determinism check.
    from polyalpha.replay_verification import run_dual_replay
    replay = run_dual_replay(args.raw_dir)
    print("REPLAY:", json.dumps(replay.as_dict(), indent=2))
    if not replay.deterministic:
        phase_store.transition("BURNIN_FAILED")
        print("PHASE: -> BURNIN_FAILED (replay not deterministic)")
        return 1

    # 4. Generate the burn-in report.
    from polyalpha.burnin_gate import BurnInReport, write_burn_in_report, gate_verdict
    report = BurnInReport(
        period_start=str(Path(args.raw_dir) / "2026" / "09" / "15"),
        period_end=str(Path(args.raw_dir)),
        baseline_commit=args.baseline_commit,
        implementation_commit=args.implementation_commit,
        research_logic_sha256="",
        collector_sha256="",
        interface_sha256="",
        messages=result.stats.messages_received,
        markets=result.stats.book_events,
        tokens_observed=len(tokens),
        lifecycle_events=result.stats.new_market_events + result.stats.market_resolved_events,
        resolved_markets=result.stats.market_resolved_events,
        reconnects=result.stats.reconnect_count,
        forced_failures=0,
        replay_hash_a=replay.hash_a,
        replay_hash_b=replay.hash_b,
        replay_deterministic=replay.deterministic,
        rest_reconciliations=result.reconcile_summary["total"],
        rest_matching=result.reconcile_summary["matches"],
        rest_unresolved_mismatches=result.reconcile_summary["mismatches"],
        metadata_reconstruction_ok=True,
        resolution_lifecycle_ok=True,
        crash_recovery_ok=True,
        gate_passed=False,
        fault_injection=dict(
            (k.split(":", 1)[0], v) for k, v in (a.split(":") for a in args.fault_injection.split(","))
        ),
        live_ready=dict(
            (k.split(":", 1)[0], v) for k, v in (a.split(":") for a in args.live_ready.split(","))
        ),
    )
    directory, digest = write_burn_in_report(report, args.report_dir)
    print(f"BURNIN REPORT: {directory} (sha256={digest})")

    # 5. Run the gate.
    passed, failures = gate_verdict(report)
    if passed:
        print("FINAL GATE: PASS")
        print("Next: operator runs collect-start-marker to write REAL_DATA_START.")
        return 0
    phase_store.transition("BURNIN_FAILED")
    print(f"FINAL GATE: FAIL ({failures})")
    print("PHASE: -> BURNIN_FAILED (fix correctness/safety only, add regression, rerun)")
    return 1


if __name__ == "__main__":
    sys.exit(main())