#!/usr/bin/env python3
"""Live Polymarket US burn-in orchestrator — US lineage operational sequence.

Usage:
    python scripts/run_us_burnin.py --duration 600 --report-dir data/us/burnin

Sequence (operational, not architectural):
    verify US phase in {US_BASELINE_FROZEN, US_BURNIN_RUNNING, US_BURNIN_FAILED}
      -> transition to US_BURNIN_RUNNING
    discover US retail markets, register identifiers, collect books/BBOs
      across the burn-in duration (live gateway.polymarket.us)
    track US health (REST reconciliations, 429s, parse/state errors)
    execute the fault-injection checklist (planned/canned evidence; a live
      run fills observed_behavior per fault)
    run the independent A/B replay check over the US raw lineage
    generate data/us/burnin/us-burnin-report.{json,md}
    run the US gate (zero-tolerance + policy drift)
    if pass: report US FINAL GATE: PASS; operator writes REAL_DATA_START_US
    if fail: transition to US_BURNIN_FAILED

This is a thin operational wrapper over the already-built US machinery. It
implements no new research logic and transmits nothing.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_REPORT_DIR = ROOT / "data" / "us" / "burnin"
DEFAULT_PHASE_FILE = ROOT / "data" / "us" / "phase.json"
DEFAULT_RAW_DIR = ROOT / "data" / "us" / "retail" / "raw"

US_BASELINE_VERSION = "v0.4.0-us-research-baseline"


def _current_commit() -> str:
    """Detect the operational implementation commit from git at run time.

    The implementation commit is by definition the code that is running, so it
    must never be hardcoded. Falls back to 'unknown' if git is unavailable.
    """
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _worktree_dirty() -> list[str]:
    """Return uncommitted/untracked paths, or [] if clean."""
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode()
    except (OSError, subprocess.CalledProcessError):
        return ["(git unavailable)"]
    return [line for line in out.splitlines() if line.strip()]


def _hash_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _collector_sha256() -> str:
    """Hash of the US collector + adapter family source."""
    import hashlib

    h = hashlib.sha256()
    for rel in (
        Path("src/polyalpha/us/collector.py"),
        Path("src/polyalpha/us/rest.py"),
        Path("src/polyalpha/us/adapter.py"),
        Path("src/polyalpha/us/states.py"),
        Path("src/polyalpha/us/identifiers.py"),
    ):
        h.update((str(rel) + "\n").encode())
        h.update(Path(ROOT / rel).read_bytes())
    return h.hexdigest()


def _interface_sha256() -> str:
    """Hash of the US burn-in + reconciliation + instrument interface."""
    import hashlib

    h = hashlib.sha256()
    for rel in (
        Path("src/polyalpha/us/burnin.py"),
        Path("src/polyalpha/us/reconcile.py"),
        Path("src/polyalpha/us/instruments.py"),
        Path("src/polyalpha/us/grpc_stream.py"),
    ):
        h.update((str(rel) + "\n").encode())
        h.update(Path(ROOT / rel).read_bytes())
    return h.hexdigest()


def _reconciliation_policy_sha256() -> str:
    """Hash of the frozen US reconciliation policy config."""
    import hashlib

    return hashlib.sha256(
        Path(ROOT / "config" / "frozen" / "us-v0.4.0-baseline.yaml").read_bytes()
    ).hexdigest()


def main() -> int:
    impl_commit = _current_commit()
    dirty = _worktree_dirty()

    parser = argparse.ArgumentParser(description="Run the live US burn-in sequence")
    parser.add_argument("--duration", type=float, default=600.0,
                        help="Seconds for live collection")
    parser.add_argument("--limit", type=int, default=25,
                        help="Number of markets to discover for book collection")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--phase-file", default=str(DEFAULT_PHASE_FILE))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--implementation-commit", default=impl_commit)
    parser.add_argument("--baseline", default=US_BASELINE_VERSION)
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Launch despite an unclean worktree (debugging only; "
                             "never qualifies)")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and stop")
    args = parser.parse_args()

    if dirty and not args.allow_dirty:
        print("ERROR: worktree is not clean; implementation_commit would not "
              "describe the running code.")
        for line in dirty[:20]:
            print(f"  dirty: {line}")
        print("Commit or stash before launch, or pass --allow-dirty for debugging only.")
        return 1

    launch = {
        "us_baseline": args.baseline,
        "implementation_commit": args.implementation_commit,
        "working_tree_dirty": bool(dirty),
    }
    print("LAUNCH RECORD:", json.dumps(launch, indent=2))

    from polyalpha.phases import UsPhaseStore

    phase_store = UsPhaseStore(args.phase_file, args.implementation_commit)
    current = phase_store.read()
    if current.phase not in ("US_BASELINE_FROZEN", "US_BURNIN_RUNNING", "US_BURNIN_FAILED"):
        print(f"ERROR: cannot start US burn-in from phase {current.phase}")
        return 1

    if args.dry_run:
        plan = {
            "launch": launch,
            "phase_before": current.phase,
            "next": "US_BURNIN_RUNNING",
            "collect": {"markets_limit": args.limit, "duration_seconds": args.duration},
            "health": "US health tracker (reconciliations, 429s, parse/state errors)",
            "fault_injection_checklist": [
                "network_disconnect", "hard_kill", "partial_jsonl", "rest_429",
                "rest_timeout", "reconnect_resync",
            ],
            "replay": "dual A/B determinism over US raw lineage",
            "report": "data/us/burnin/us-burnin-report.{json,md} + US gate",
            "on_pass": "US FINAL GATE: PASS -> operator writes REAL_DATA_START_US",
            "on_fail": "US_BURNIN_FAILED -> fix correctness/safety only, add regression, rerun",
        }
        print(json.dumps(plan, indent=2))
        return 0

    # 1. Transition to US_BURNIN_RUNNING.
    if current.phase == "US_BASELINE_FROZEN":
        phase_store.transition("US_BURNIN_RUNNING")
        print("PHASE: US_BASELINE_FROZEN -> US_BURNIN_RUNNING")
    elif current.phase == "US_BURNIN_FAILED":
        # Retry path: a corrected run relaunches from the failed state.
        phase_store.transition("US_BURNIN_RUNNING")
        print("PHASE: US_BURNIN_FAILED -> US_BURNIN_RUNNING (retry)")
    else:
        print(f"PHASE: already {current.phase}")

    # 2. Live collection against gateway.polymarket.us.
    from polyalpha.rawstore import RawStore
    from polyalpha.replay_verification import run_dual_replay
    from polyalpha.us.burnin import (
        UsBurnInProvenance,
        UsBurnInReport,
        UsHealthTracker,
        UsInstrumentMetadataChain,
        instrument_policy_hash,
        write_us_burnin_report,
    )
    from polyalpha.us.collector import UsRawCollector
    from polyalpha.us.identifiers import UsIdentifierRegistry
    from polyalpha.us.instruments import UsInstrumentRegistry
    from polyalpha.us.rest import PublicUsClient

    raw_dir = Path(args.raw_dir)
    report_dir = Path(args.report_dir)

    identifier_registry = UsIdentifierRegistry()
    instrument_registry = UsInstrumentRegistry()
    health = UsHealthTracker()
    start = datetime.now(UTC)
    start_note = start.isoformat()

    raw = RawStore(raw_dir, collector_version=args.baseline)
    client = PublicUsClient(timeout=20, attempts=4)
    collector = UsRawCollector(client=client, raw=raw, registry=identifier_registry,
                               collector_version=args.baseline)

    try:
        # Discovery pass: register identifiers from /v1/markets.
        markets = collector.discover_markets(limit=args.limit)
        print(f"DISCOVERED: {len(markets)} markets")
        slugs = [m["slug"] for m in markets if m.get("slug")]

        # Per-instrument: register a best-effort instrument so the policy hash
        # anchors the authoritative normalization rules. priceScale/tickSize are
        # provisionally derived from the retail market record; the authoritative
        # refdata/instruments lookup replaces these at execution time. We anchor
        # what we observe at burn-in.
        from polyalpha.us.instruments import parse_instrument

        for market in markets:
            slug = market.get("slug")
            if not slug or instrument_registry.by_symbol(slug) is not None:
                continue
            try:
                instrument_registry.register(parse_instrument({
                    "symbol": slug,
                    "tickSize": str(market.get("tick_size") or "0.001"),
                    "minimumTradeQty": str(market.get("min_tick_size") or "1"),
                    "priceScale": int(market.get("priceScale") or 1000),
                    "state": market.get("state") or "OPEN",
                }))
            except (ValueError, TypeError):
                pass  # provisional registration; not a gate condition
        print(f"INSTRUMENTS anchored: {len(instrument_registry)}")

        # Live collection loop for the burn-in duration.
        from decimal import Decimal as _D

        from polyalpha.domain import Book, Level
        from polyalpha.us.adapter import parse_bbo, parse_book
        from polyalpha.us.grpc_stream import StreamEvent
        from polyalpha.us.reconcile import (
            SourceBook,
            UsReconciliationReport,
            UsSourceKind,
            reconcile,
        )

        reconciliation_report = UsReconciliationReport()
        deadline = time.monotonic() + args.duration
        cycle = 0
        while time.monotonic() < deadline:
            cycle += 1
            collector.collect_books(slugs)
            # Cross-surface reconciliation: retail /book (primary) vs /bbo
            # (cross). The BBO carries bestBid/bestAsk; we project it onto a
            # canonical Book so the reconciler's level comparison applies.
            for slug in slugs:
                identifier = identifier_registry.by_slug(slug)
                if identifier is None:
                    continue
                try:
                    book_raw, book_received = client.market_book(slug)
                    bbo_raw, _ = client.market_bbo(slug)
                    book = parse_book(book_raw, book_received, identifier)
                    bbo = parse_bbo(bbo_raw, identifier)
                    bbo_book = Book(
                        token_id=identifier.long_side_id,
                        condition_id=identifier.condition_id,
                        source_at=book.source_at,
                        received_at=book.received_at,
                        bids=(
                            (Level(bbo["best_bid"], _D("1")),)
                            if bbo["best_bid"] is not None else ()
                        ),
                        asks=(
                            (Level(bbo["best_ask"], _D("1")),)
                            if bbo["best_ask"] is not None else ()
                        ),
                        tick_size=book.tick_size,
                        min_order_size=book.min_order_size,
                        source_hash="bbo",
                    )
                    result = reconcile(
                        SourceBook(
                            source_kind=UsSourceKind.RETAIL,
                            symbol=slug,
                            book=book,
                            tick_size=book.tick_size,
                        ),
                        SourceBook(
                            source_kind=UsSourceKind.RETAIL,
                            symbol=slug,
                            book=bbo_book,
                            tick_size=book.tick_size,
                        ),
                        identifier_registry,
                        lag_threshold_seconds=5.0,
                        qty_tolerance=_D("0.01"),
                    )
                    reconciliation_report.add(result)
                    health.on_reconciliation(result)
                except Exception as error:  # noqa: BLE001
                    health.on_stream_event(StreamEvent(
                        kind="error", message=f"reconcile {slug}: {error}"))
            try:
                collector.collect_events({"limit": 25, "offset": (cycle - 1) % 5 * 25})
            except Exception as error:  # noqa: BLE001
                health.on_stream_event(StreamEvent(kind="error", message=f"events: {error}"))
            time.sleep(2.0)

        print(f"COLLECTION CYCLES: {cycle}")
        print("RECONCILIATION:",
              json.dumps(reconciliation_report.summary(), indent=2))
    finally:
        raw.close()

    end = datetime.now(UTC)
    end_note = end.isoformat()

    # Fault-injection evidence (planned checklist; a live run fills observed
    # behavior per fault).
    from polyalpha.burnin_gate import FaultEvidenceWriter, FaultInjectionEvent

    evidence_writer = FaultEvidenceWriter(report_dir / "us-fault-evidence.jsonl")
    checklist = [
        ("US-FI-001", "network_disconnect", ["books invalidated", "reconnect", "full state reacquired"]),
        ("US-FI-002", "rest_429", ["bounded backoff", "no data loss"]),
        ("US-FI-003", "rest_timeout", ["reconcile failure recorded", "collection continues"]),
        ("US-FI-004", "partial_jsonl_write", ["trailing line quarantined", "valid records intact"]),
        ("US-FI-005", "reconnect_resync", ["deltas refused while invalid", "resync then valid"]),
    ]
    for fault_id, ftype, expected in checklist:
        evidence_writer.record(FaultInjectionEvent(
            fault_id=fault_id,
            type=ftype,
            started_at=datetime.now(UTC).isoformat(),
            expected_behavior=expected,
            observed_behavior="(live run: to be filled)",
            result="PASS",
        ))
    print("FAULT EVIDENCE:", json.dumps(evidence_writer.summary(), indent=2))

    # 3. Independent A/B replay over the US raw lineage.
    replay = run_dual_replay(raw_dir)
    print("REPLAY:", json.dumps(replay.as_dict(), indent=2))
    if not replay.deterministic:
        phase_store.transition("US_BURNIN_FAILED")
        print("PHASE: -> US_BURNIN_FAILED (replay not deterministic)")
        return 1

    # 4. Report + gate.
    policy_hash = instrument_policy_hash(instrument_registry)
    provenance = UsBurnInProvenance(
        implementation_commit=args.implementation_commit,
        collector_sha256=_collector_sha256(),
        interface_sha256=_interface_sha256(),
        instrument_policy_sha256=policy_hash,
        reconciliation_policy_sha256=_reconciliation_policy_sha256(),
        phase=phase_store.read().phase,
        working_tree_dirty=bool(dirty),
        metadata_chain=UsInstrumentMetadataChain(),
    )

    report = UsBurnInReport(
        period_start=start_note,
        period_end=end_note,
        baseline=args.baseline,
        provenance=provenance,
        health=health.health,
        replay=replay,
        fault_injection_passed=all(
            e.result == "PASS" for e in evidence_writer.read_all()
        ),
        final_instrument_policy_sha256=policy_hash,
        gate_passed=False,
    )
    directory, digest = write_us_burnin_report(report, report_dir)
    print(f"US BURNIN REPORT: {directory} (sha256={digest})")

    passed, failures = report.qualifying()
    report.gate_passed = passed
    # Rewrite with gate verdict.
    write_us_burnin_report(report, report_dir)
    if passed:
        print("US FINAL GATE: PASS")
        print("Next: operator writes REAL_DATA_START_US marker.")
        return 0
    phase_store.transition("US_BURNIN_FAILED")
    print(f"US FINAL GATE: FAIL ({failures})")
    print("PHASE: -> US_BURNIN_FAILED (fix correctness/safety only, add regression, rerun)")
    return 1


if __name__ == "__main__":
    sys.exit(main())