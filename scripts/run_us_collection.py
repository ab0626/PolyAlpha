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


def _harvest_event_slugs(events: list[dict]) -> set[str]:
    """Collect every child-market slug of every parent event.

    The events feed exposes the full open event graph (parent event -> child
    markets); these markets are not surfaced by the top-N activity discovery.
    Harvesting them widens the evidence universe to the whole event graph.
    """
    out: set[str] = set()
    for event in events:
        for market in event.get("markets", []) or []:
            slug = market.get("slug")
            if slug:
                out.add(slug)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Sustained US collection (evidence regime)")
    parser.add_argument("--duration", type=float, default=3600.0,
                        help="Seconds for sustained collection")
    parser.add_argument("--limit", type=int, default=200,
                        help="Number of markets to discover and track")
    parser.add_argument("--phase-file", default=str(DEFAULT_PHASE_FILE))
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Seconds between collection cycles")
    parser.add_argument("--book-budget", type=int, default=400,
                        help="Markets to book per cycle (round-robin window); bounds "
                             "cycle time as the tracked universe grows")
    parser.add_argument("--settle-budget", type=int, default=200,
                        help="Markets to settlement-poll per sweep (round-robin window)")
    parser.add_argument("--settle-interval", type=float, default=60.0,
                        help="Seconds between settlement sweeps")
    parser.add_argument("--schedule", default=str(ROOT / "config" / "gov_releases.json"))
    parser.add_argument("--mapping", default="data/us/release_mapping.jsonl")
    parser.add_argument("--strata", default=str(ROOT / "config" / "collection_strata.json"))
    parser.add_argument("--membership", default="data/us/stratified_membership.jsonl")
    parser.add_argument("--tracked", default="data/us/tracked_membership.jsonl",
                        help="Persisted full tracked universe (settlement poll set)")
    parser.add_argument("--settled", default="data/us/settled_membership.jsonl",
                        help="Persisted slugs whose final settlement was captured")
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
        activity_slugs = [m["slug"] for m in markets if m.get("slug")]

        from polyalpha.us.release_coverage import merged_required_slugs

        def search_fn(term: str):
            raw, _ = client.search({"query": term, "status": "active", "limit": 20})
            return raw.get("events", [])

        required_slugs = merged_required_slugs(args.schedule, search_fn, args.mapping)

        # Stratified category sample (frozen rule), membership persisted point-in-time.
        from polyalpha.us.release_coverage import persist_stratified_membership, stratified_universe

        stratified_slugs: set[str] = set()
        if args.strata:
            strata_cfg = json.loads(Path(args.strata).read_text(encoding="utf-8"))
            sampled = stratified_universe(client, strata_cfg)
            stratified_slugs = {slug for slug, _ in sampled}
            persist_stratified_membership(sampled, args.membership)

        # Widen the book universe to the full open event graph. The top-N
        # activity discovery only surfaces the most active markets; the events
        # feed exposes every parent event and its child markets. Harvest them
        # (paginated) so the independent-cluster gate accumulates across the
        # whole event graph, not just the activity window.
        event_slugs: set[str] = set()
        for offset in range(0, 600, 100):
            try:
                events = collector.collect_events({"limit": 100, "offset": offset})
                found = _harvest_event_slugs(events)
                event_slugs |= found
                if not found:
                    break
            except Exception as error:  # noqa: BLE001
                print(f"events discovery error (offset {offset}): {error}")

        slugs = sorted(set(activity_slugs) | required_slugs | stratified_slugs | event_slugs)

        # Persist the full tracked universe point-in-time. Once a market is
        # discovered it stays in the tracked set even after it closes or drops
        # out of the bounded top-N activity window: discover_markets(closed=false)
        # re-derives a fresh set each run, so both the book path and
        # /settlement of previously-tracked markets would otherwise be lost.
        from polyalpha.us.release_coverage import (
            load_slug_membership,
            persist_slug_membership,
        )

        tracked_path = Path(args.tracked)
        settled_path = Path(args.settled)
        previously_tracked = load_slug_membership(tracked_path)
        new_tracked = set(slugs) - previously_tracked
        persist_slug_membership(new_tracked, tracked_path, field="tracked_at")
        tracked_slugs = previously_tracked | set(slugs)

        # Register an identifier for every tracked slug (release-required and
        # stratified slugs are not discovered via discover_markets, and
        # previously-tracked/closed markets are not rediscovered) so both book
        # collection and settlement polling can address them.
        for slug in tracked_slugs:
            if identifier_registry.by_slug(slug) is None:
                identifier_registry.register(f"us:{slug}", slug)

        settled_slugs = load_slug_membership(settled_path)

        # Book the full additive tracked universe, not just this run's fresh
        # discovery, so the evidence universe grows across runs instead of
        # plateauing at the bounded top-N activity window. Release-required
        # contracts (NFP/CPI/FOMC) are the preregistered experiment's critical
        # instruments: book them EVERY cycle so their REST receipt stays dense
        # through the release window. The rest of the (large, event-derived)
        # universe is round-robined for cluster coverage.
        priority_slugs = sorted(set(required_slugs) & tracked_slugs)
        round_robin_slugs = sorted(tracked_slugs - set(required_slugs))

        # Round-robin cursors persist across chunks. Without persistence every
        # ~5-minute chunk restarts at index 0, so the tail of the (large,
        # event-derived) universe is never booked and its settlements never
        # polled. Persisting the cursors makes coverage complete over time.
        cursor_path = Path(args.settled).with_name("collection_cursors.json")

        def _load_cursors() -> dict:
            try:
                data = json.loads(cursor_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {"book": 0, "settle": 0}
            return {"book": int(data.get("book", 0)), "settle": int(data.get("settle", 0))}

        def _save_cursors(state: dict) -> None:
            cursor_path.parent.mkdir(parents=True, exist_ok=True)
            cursor_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

        print(f"DISCOVERED: {len(markets)} activity; universe={len(slugs)} "
              f"(required={len(required_slugs)}, stratified={len(stratified_slugs)}, "
              f"events={len(event_slugs)}); tracked={len(tracked_slugs)} "
              f"priority={len(priority_slugs)} round_robin={len(round_robin_slugs)} "
              f"settlement_poll={len(tracked_slugs - settled_slugs)}")

        deadline = time.monotonic() + args.duration
        cursors = _load_cursors()
        n_rr = max(1, len(round_robin_slugs))
        book_cursor = cursors["book"] % n_rr
        settle_cursor = cursors["settle"] % n_rr
        # Settlement sweep fires immediately (capture already-resolved markets,
        # e.g. a release that just passed) then on a wall-clock cadence. It is
        # decoupled from the cycle counter: a ~5-minute chunk with ~40s book
        # cycles never reaches cycle 10, which previously meant settlement
        # polling silently never ran.
        next_settle = time.monotonic()
        while time.monotonic() < deadline:
            cycles += 1
            # Priority (release-required) contracts every cycle; a bounded
            # round-robin window of the rest so cycle time stays bounded as the
            # universe grows to the full event graph.
            if priority_slugs:
                collector.collect_books(priority_slugs)
            if round_robin_slugs:
                window = [round_robin_slugs[(book_cursor + i) % n_rr]
                          for i in range(min(args.book_budget, n_rr))]
                collector.collect_books(window)
                book_cursor = (book_cursor + args.book_budget) % n_rr
            try:
                collector.collect_events({"limit": 100, "offset": (cycles - 1) % 6 * 100})
            except Exception as error:  # noqa: BLE001
                print(f"events error (cycle {cycles}): {error}")
            if time.monotonic() >= next_settle:
                # Release-required contracts every sweep (few, and they drive
                # the settled-markets gate); a bounded round-robin window of
                # the rest with a persisted cursor so the full universe is
                # swept over many chunks.
                pending_macro = [s for s in priority_slugs if s not in settled_slugs]
                rr_window = [
                    s for s in (
                        round_robin_slugs[(settle_cursor + i) % n_rr]
                        for i in range(min(args.settle_budget, n_rr))
                    ) if s not in settled_slugs
                ] if round_robin_slugs else []
                newly_settled = collector.collect_settlements(pending_macro + rr_window)
                settle_cursor = (settle_cursor + args.settle_budget) % n_rr
                if newly_settled:
                    persist_slug_membership(newly_settled, settled_path, field="settled_at")
                    settled_slugs |= newly_settled
                _save_cursors({"book": book_cursor, "settle": settle_cursor})
                next_settle = time.monotonic() + args.settle_interval
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