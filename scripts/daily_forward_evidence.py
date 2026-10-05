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

from polyalpha.notify import ThrottledToaster  # noqa: E402
from polyalpha.us.forward_evidence import build_report_from_raw  # noqa: E402

# The research dataset is built by two full passes over the raw store (metadata
# pass, then book/snapshot pass); progress is reported as a fraction of both.
N_PASSES = 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily forward-evidence snapshot")
    parser.add_argument("--raw-dir", default="data/us/retail/raw")
    parser.add_argument("--release-mapping", default="data/us/release_mapping.jsonl",
                        help="Path to release_mapping.jsonl (macro parent-event grouping)")
    parser.add_argument("--report-dir", default="data/us/reports")
    parser.add_argument("--log-file", default="data/us/logs/forward-evidence.log")
    parser.add_argument("--notify", action="store_true",
                        help="Pop periodic Windows desktop toasts with replay progress")
    parser.add_argument("--notify-interval-minutes", type=float, default=5.0,
                        help="Minutes between progress toasts (default 5)")
    args = parser.parse_args()

    progress = None
    if args.notify:
        toaster = ThrottledToaster(interval_seconds=args.notify_interval_minutes * 60)

        def _progress(pass_idx: int, done: int, total: int) -> None:
            frac = (pass_idx + (done / total if total else 0.0)) / N_PASSES
            toaster.maybe(
                "Forward-evidence replay",
                f"{frac * 100.0:.0f}%  (pass {pass_idx + 1}/{N_PASSES}, "
                f"{done:,}/{total:,} files)",
            )

        progress = _progress

    report = build_report_from_raw(
        args.raw_dir, release_mapping_path=args.release_mapping, progress=progress
    )
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

    if args.notify:
        toaster.maybe(
            "Forward-evidence replay done",
            f"settled {gates['settled_markets']['value']}, "
            f"clusters {gates['independent_clusters']['value']}, "
            f"days {gates['days']['value']}",
            force=True,
        )

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
