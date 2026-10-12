#!/usr/bin/env python3
"""Bounded, instrumented raw-store replay diagnostic.

Measures the true cost of replaying the US retail raw store WITHOUT building the
research dataset: per authoritative file it reports index/total, filename,
compressed bytes, records parsed, per-file elapsed, cumulative records/bytes, and
a running elapsed time. A watchdog thread aborts visibly if no file completes
within ``--watchdog-seconds``.

Read-only: it never modifies the store, never writes, and never touches dataset
semantics. It uses the same file discovery + parsing path as ``RawStore.replay``
so the timing is representative of the mainline ``--refresh`` replay.

Usage:
    .venv/Scripts/python.exe scripts/replay_diagnostic.py --max-seconds 120
    .venv/Scripts/python.exe scripts/replay_diagnostic.py --max-files 50
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.rawstore import RawStore  # noqa: E402


def _discover(raw: RawStore) -> list[Path]:
    files: list[Path] = []
    for directory in sorted(p for p in raw.root.rglob("*") if p.is_dir()):
        files.extend(raw._day_files(directory))
    return sorted(set(files))


class _Watchdog:
    """Abort if `tick()` is not called for `timeout` seconds."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self._event = threading.Event()
        self._dead = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._dead:
            if self._event.wait(self.timeout):
                self._event.clear()
                continue
            print(
                f"\n[WATCHDOG] no file completed for {self.timeout:.0f}s — "
                f"replay appears hung; aborting.",
                flush=True,
            )
            sys.exit(1)

    def start(self) -> None:
        self._thread.start()

    def tick(self) -> None:
        self._event.set()

    def stop(self) -> None:
        self._dead = True
        self._event.set()


def main() -> int:
    ap = argparse.ArgumentParser(description="Bounded, instrumented raw-store replay")
    ap.add_argument("--raw-dir", default="data/us/retail/raw")
    ap.add_argument("--max-files", type=int, default=0, help="stop after N files (0 = all)")
    ap.add_argument("--max-seconds", type=float, default=0.0,
                    help="stop after N seconds (0 = no limit)")
    ap.add_argument("--watchdog-seconds", type=float, default=300.0,
                    help="abort if no file completes within N seconds")
    ap.add_argument("--every", type=int, default=10, help="print a line every N files")
    args = ap.parse_args()

    raw = RawStore(args.raw_dir)
    files = _discover(raw)
    total = len(files)
    total_bytes = sum(f.stat().st_size for f in files if f.exists())
    print(f"files={total}  total_compressed_MB={total_bytes/1e6:.0f}", flush=True)

    wd = _Watchdog(args.watchdog_seconds)
    wd.start()

    t0 = time.time()
    n_records = 0
    n_bytes = 0
    for i, f in enumerate(files):
        sz = f.stat().st_size if f.exists() else 0
        n_bytes += sz
        recs = 0
        try:
            handle = gzip.open(f, "rt", encoding="utf-8") if f.suffix == ".gz" else open(f, "r", encoding="utf-8")
        except OSError:
            print(f"[{i+1}/{total}] SKIP {f} (transient)", flush=True)
            wd.tick()
            continue
        fstart = time.time()
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    json.loads(data["canonical_json"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                recs += 1
        n_records += recs
        wd.tick()
        elapsed = time.time() - fstart
        if (i + 1) % args.every == 0 or i == total - 1:
            wall = time.time() - t0
            rate = (n_bytes / 1e6 / wall) if wall > 0 else 0.0
            print(
                f"[{i+1}/{total}] {f.name} bytes={sz} recs={recs} file_s={elapsed:.2f} "
                f"cum_MB={n_bytes/1e6:.0f} cum_recs={n_records} rate_MBps={rate:.1f} "
                f"wall_s={wall:.0f}",
                flush=True,
            )
        if args.max_files and (i + 1) >= args.max_files:
            print(f"stopped at --max-files {args.max_files}", flush=True)
            break
        if args.max_seconds and (time.time() - t0) >= args.max_seconds:
            print(f"stopped at --max-seconds {args.max_seconds}", flush=True)
            break

    wall = time.time() - t0
    print(
        f"DONE records={n_records} compressed_MB={n_bytes/1e6:.0f} "
        f"wall_s={wall:.1f} avg_rate_MBps={n_bytes/1e6/wall:.1f}",
        flush=True,
    )
    wd.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())