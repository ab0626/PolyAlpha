#!/usr/bin/env python3
"""Reconcile the watchdog gap journal into candidate ledger additions.

Reads the watchdog's append-only operational journal and produces the *exact*
candidate additions for ``config/us_known_gaps.json``:

  * pairs ``outage_open``/``outage_close`` events into closed intervals,
  * unions overlaps (never double-counts),
  * subtracts intervals already covered by the canonical ledger (idempotent),
  * emits only fully-closed, mechanically-derived intervals.

The canonical research ledger is NOT mutated by default (dry-run). Use
``--apply`` to write the candidate additions — a human-reviewed step, never
invoked automatically by the watchdog daemon.

Usage:
    python scripts/reconcile_us_gaps.py              # dry-run: print candidates
    python scripts/reconcile_us_gaps.py --json       # machine-readable dry-run
    python scripts/reconcile_us_gaps.py --apply      # write to the ledger
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.us.gap_watchdog import GapJournal, build_candidates, parse_iso  # noqa: E402

DEFAULT_EVENTS = ROOT / "data" / "us" / "gap_watchdog" / "events.jsonl"
DEFAULT_LEDGER = ROOT / "config" / "us_known_gaps.json"


def _load_ledger_gaps(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("gaps", []) if isinstance(data, dict) else []


def _apply(ledger_path: Path, candidates: list[dict]) -> int:
    merged = _load_ledger_gaps(ledger_path) + candidates
    data = {"gaps": merged}
    tmp = ledger_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, ledger_path)
    print(f"APPLIED {len(candidates)} candidate(s) -> {ledger_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile watchdog gaps into ledger candidates")
    ap.add_argument("--events-file", default=str(DEFAULT_EVENTS))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    ap.add_argument("--apply", action="store_true",
                    help="Write candidates to the canonical ledger (human-reviewed step)")
    ap.add_argument("--reason", default=None,
                    help="Override the candidate reason with a human-reviewed judgment "
                         "(e.g. 'controlled watchdog recovery test')")
    args = ap.parse_args()

    events = GapJournal(args.events_file).events()
    existing_gaps = _load_ledger_gaps(Path(args.ledger))
    candidates, still_open = build_candidates(events, existing_gaps)
    if args.reason:
        for c in candidates:
            c["reason"] = args.reason

    if args.json:
        print(json.dumps({
            "candidates": candidates,
            "still_open": still_open,
            "candidate_count": len(candidates),
        }, indent=2, sort_keys=True))
        return 0

    print("Watchdog gap reconciliation")
    print("----------------------------")
    print(f"  events:             {len(events)}")
    print(f"  closed intervals:   {len(candidates)} candidate addition(s)")
    print(f"  still-open:         {len(still_open)} interval(s) (NOT applied)")
    print()
    if candidates:
        print("Candidate additions (for human review):")
        for c in candidates:
            lo = parse_iso(c["start"])
            hi = parse_iso(c["end"])
            dur = (hi - lo).total_seconds() / 3600.0 if lo and hi else 0.0
            print(f"  {c['id']}  {c['start']} -> {c['end']}  ({dur:.2f}h)  {c['reason']}")
    else:
        print("No new closed intervals to add.")
    print()
    if still_open:
        print("Still open (excluded from any apply; will be candidates once closed):")
        for o in still_open:
            print(f"  gap_start={o.get('gap_start')} reason={o.get('reason')}")

    if args.apply:
        if not candidates:
            print("\nNothing to apply.")
            return 0
        return _apply(Path(args.ledger), candidates)

    print("\nDry-run: no changes written to the canonical ledger.")
    return 0


if __name__ == "__main__":
    sys.exit(main())