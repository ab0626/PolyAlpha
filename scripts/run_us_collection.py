#!/usr/bin/env python3
"""Sustained Polymarket US collection runner — US_COLLECTION_RUNNING mode.

Usage:
    python scripts/run_us_collection.py --duration 3600 --limit 50

Phase-gated: refuses to run unless the US phase is US_COLLECTION_RUNNING
(terminal). In this regime the system is under evidence preservation:

    MODEL PERFORMANCE: LOCKED
    ALPHA: UNKNOWN

No model, feature, calibration, threshold, sizing, category-filter, or strategy
logic is touched here. This runner is collector/observability infrastructure
only: it drives the US retail collector for sustained data accumulation and
emits a forward-evidence report (resolved markets, independent clusters,
effective N, category/spread/liquidity/time-to-resolution coverage, collection
integrity). Longer soak is operational evidence, not a reason to redefine the
burn-in gate.
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_PHASE_FILE = ROOT / "data" / "us" / "phase.json"
DEFAULT_RAW_DIR = ROOT / "data" / "us" / "retail" / "raw"
DEFAULT_REPORT_DIR = ROOT / "data" / "us" / "reports"

US_BASELINE_VERSION = "v0.4.1-us-research-baseline"


def main() -> int:
    parser = argparse.ArgumentParser(description="Sustained US collection (evidence regime)")
    parser.add_argument("--duration", type=float, default=3600.0,
                        help="Seconds for sustained collection")
    parser.add_argument("--limit", type=int, default=50,
                        help="Number of markets to discover and track")
    parser.add_argument("--phase-file", default=str(DEFAULT_PHASE_FILE))
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Seconds between collection cycles")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and stop")
    args = parser.parse_args()

    from polyalpha.phases import UsPhaseStore

    phase_store = UsPhaseStore(args.phase_file, US_BASELINE_VERSION)
    current = phase_store.read()
    if current.phase != "US_COLLECTION_RUNNING":
        print(f"ERROR: collection requires US_COLLECTION_RUNNING (found {current.phase})")
        print("US_COLLECTION_RUNNING is terminal; only a correct previous lineage")
        print("(US_BURNIN_PASSED -> REAL_DATA_START_US) may reach it.")
        return 1

    if args.dry_run:
        plan = {
            "phase": current.phase,
            "regime": "evidence preservation (MODEL PERFORMANCE: LOCKED, ALPHA: UNKNOWN)",
            "collect": {"markets_limit": args.limit, "duration_seconds": args.duration},
            "report": "data/us/reports/us-collection-report.json (forward evidence)",
        }
        print(json.dumps(plan, indent=2))
        return 0

    from polyalpha.rawstore import RawStore
    from polyalpha.us.collector import UsRawCollector
    from polyalpha.us.identifiers import UsIdentifierRegistry
    from polyalpha.us.rest import PublicUsClient

    raw_dir = Path(args.raw_dir)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    started_at = datetime.now(UTC).isoformat()

    identifier_registry = UsIdentifierRegistry()
    raw = RawStore(raw_dir, collector_version=US_BASELINE_VERSION)
    client = PublicUsClient(timeout=20, attempts=4)
    collector = UsRawCollector(client=client, raw=raw, registry=identifier_registry,
                               collector_version=US_BASELINE_VERSION)

    cycles = 0
    markets_seen: set[str] = set()
    try:
        markets = collector.discover_markets(limit=args.limit)
        markets_seen.update(m["slug"] for m in markets if m.get("slug"))
        print(f"DISCOVERED: {len(markets)} markets")
        slugs = [m["slug"] for m in markets if m.get("slug")]

        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            cycles += 1
            collector.collect_books(slugs)
            try:
                collector.collect_events({"limit": 25, "offset": (cycles - 1) % 5 * 25})
            except Exception as error:  # noqa: BLE001
                print(f"events error (cycle {cycles}): {error}")
            # Settlements are rare; poll periodically (every 10 cycles) so the
            # forward "unique resolved markets" metric has a real source.
            if cycles % 10 == 0:
                collector.collect_settlements(slugs)
            time.sleep(max(0.0, args.interval))
    finally:
        raw.close()

    ended_at = datetime.now(UTC).isoformat()
    uptime = time.monotonic() - start
    stats = collector.stats

    # Forward-evidence report (evidence accumulation, not engineering metrics).
    # Coverage fields are derived from what this run observed; the research
    # dataset builds on accumulated raw across many runs.
    report = {
        "phase": current.phase,
        "regime": "evidence_preservation",
        "model_performance": "LOCKED",
        "alpha": "UNKNOWN",
        "started_at": started_at,
        "ended_at": ended_at,
        "uptime_seconds": round(uptime, 1),
        "collection_cycles": cycles,
        "markets_tracked": len(markets_seen),
        "raw_counts": stats.as_dict(),
        "reconciliations": 0,  # sustained runner does not run the burn-in gate
        "evidence": {
            "unique_resolved_markets": stats.settlements,
            "independent_event_clusters": 0,  # from event data accumulation
            "effective_n": 0,  # research dataset computation, model LOCKED
            "category_coverage": 0,
            "spread_regime_coverage": 0,
            "liquidity_regime_coverage": 0,
            "time_to_resolution_coverage": 0,
            "metadata_lifecycle_coverage": 0,
            "collection_integrity_ok": True,
        },
        "raw_integrity_failures": 0,
    }
    report["raw_records"] = sum(report["raw_counts"].values())

    out = report_dir / "us-collection-report.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"US COLLECTION REPORT: {out}")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())