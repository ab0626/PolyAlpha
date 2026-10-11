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
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.notify import ThrottledToaster  # noqa: E402
from polyalpha.us.forward_evidence import build_report_from_raw  # noqa: E402

# The research dataset is built by two full passes over the raw store (metadata
# pass, then book/snapshot pass); progress is reported as a fraction of both.
N_PASSES = 2


class NoProgressWatchdog:
    """Abort the process if the replay stops making progress.

    The replay is a long (tens of minutes) local job; a single wedged file or a
    pathological path must fail visibly rather than appear hung. `tick()` is
    called once per file; if it is not called for `timeout_seconds`, the process
    exits non-zero with a clear message. Read-only: it only observes progress,
    it never changes replay semantics.
    """

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        self._event = threading.Event()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stopped:
            if self._event.wait(self.timeout_seconds):
                self._event.clear()
                continue
            print(
                f"FATAL: no replay progress for {self.timeout_seconds / 60.0:.1f} min "
                f"— aborting rather than appearing hung.",
                file=sys.stderr,
                flush=True,
            )
            os._exit(1)

    def start(self) -> None:
        self._thread.start()

    def tick(self) -> None:
        self._event.set()

    def stop(self) -> None:
        self._stopped = True
        self._event.set()


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
    parser.add_argument("--progress-interval-seconds", type=float, default=30.0,
                        help="Seconds between stdout progress lines (default 30)")
    parser.add_argument("--watchdog-minutes", type=float, default=15.0,
                        help="Abort if no replay progress for this many minutes "
                             "(default 15; 0 disables)")
    args = parser.parse_args()

    watchdog = None
    if args.watchdog_minutes and args.watchdog_minutes > 0:
        watchdog = NoProgressWatchdog(args.watchdog_minutes * 60)
        watchdog.start()

    toaster = None
    if args.notify:
        toaster = ThrottledToaster(interval_seconds=args.notify_interval_minutes * 60)

    start = time.time()
    last_stdout = [0.0]

    def _progress(pass_idx: int, done: int, total: int) -> None:
        if watchdog is not None:
            watchdog.tick()
        frac = (pass_idx + (done / total if total else 0.0)) / N_PASSES
        now = time.time()
        if now - last_stdout[0] >= args.progress_interval_seconds or (total and done >= total):
            last_stdout[0] = now
            print(
                f"[replay] pass {pass_idx + 1}/{N_PASSES}  {done:,}/{total:,} files "
                f"({frac * 100.0:.0f}%)  elapsed {now - start:.0f}s",
                flush=True,
            )
        if toaster is not None:
            toaster.maybe(
                "Forward-evidence replay",
                f"{frac * 100.0:.0f}%  (pass {pass_idx + 1}/{N_PASSES}, "
                f"{done:,}/{total:,} files)",
            )

    report = build_report_from_raw(
        args.raw_dir, release_mapping_path=args.release_mapping, progress=_progress
    )
    if watchdog is not None:
        watchdog.stop()
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
