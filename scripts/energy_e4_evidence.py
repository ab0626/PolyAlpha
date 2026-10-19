#!/usr/bin/env python3
"""E4 evidence classifier — derived, read-only (no model/signal/execution).

Classifies the append-only ERCOT SPP observation store against the exact E4 spec
commit time and appends derived evidence rows to a SEPARATE stream. It never
rewrites observations.jsonl. UNRESOLVED rows fail closed and never qualify for
forward evidence.

Usage:
    .venv/Scripts/python.exe scripts/energy_e4_evidence.py
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.e4_evidence import (  # noqa: E402
    E4_CUTOVER_AT,
    E4_SPEC_COMMIT,
    append_evidence,
    classify_store,
)

OBSERVATIONS = ROOT / "data" / "energy" / "e4" / "ercot_spp" / "observations.jsonl"
EVIDENCE = ROOT / "data" / "energy" / "e4" / "ercot_spp" / "evidence.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser(description="E4 evidence classifier (read-only)")
    ap.add_argument("--observations", default=str(OBSERVATIONS))
    ap.add_argument("--out", default=str(EVIDENCE))
    args = ap.parse_args()

    records = classify_store(args.observations)
    appended, seen = append_evidence(args.out, records)

    counts = Counter(r["evidence_class"] for r in records)
    forward = sum(1 for r in records if r["qualifies_forward_e4"])

    print("=== E4 EVIDENCE CLASSIFIER (derived, read-only) ===")
    print(f"e4_spec_commit : {E4_SPEC_COMMIT}")
    print(f"e4_cutover_at  : {E4_CUTOVER_AT.isoformat()}")
    print(f"observations   : {len(records)}")
    print(f"appended       : {appended} (seen {seen})")
    print()
    print("evidence class counts:")
    for cls, n in sorted(counts.items()):
        print(f"  {cls:36s} {n}")
    print()
    print(f"qualifies_forward_e4 (PROSPECTIVE_FIRST_SEEN): {forward}")
    print(f"WROTE {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())