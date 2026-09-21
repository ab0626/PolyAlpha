#!/usr/bin/env python3
"""Daily forward-evidence snapshot.

Builds the US research dataset from the raw store, computes the forward-evidence
report, writes a timestamped JSON snapshot, and appends a one-line progress
summary to a log. Runs the day's report on demand; a launcher loops it daily.

Usage:
    python scripts/daily_forward_evidence.py \
        --raw-dir data/us/retail/raw \
        --report-dir data/us/reports \
        --log-file data/us/logs/forward-evidence.log
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.us.forward_evidence import build_report_from_raw  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily forward-evidence snapshot")
    parser.add_argument("--raw-dir", default="data/us/retail/raw")
    parser.add_argument("--report-dir", default="data/us/reports")
    parser.add_argument("--log-file", default="data/us/logs/forward-evidence.log")
    args = parser.parse_args()

    report = build_report_from_raw(args.raw_dir)
    now = datetime.now(UTC)

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"forward-evidence-{now:%Y-%m-%d}.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    gates = report["gates"]
    summary = {
        "at": now.isoformat(),
        "days_elapsed": gates["days"]["value"],
        "settled_markets": gates["settled_markets"]["value"],
        "independent_clusters": gates["independent_clusters"]["value"],
        "boundary_met": report["evaluation_boundary_met"],
        "snapshot": str(out),
    }
    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, sort_keys=True) + "\n")

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
