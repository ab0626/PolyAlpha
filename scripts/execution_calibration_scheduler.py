#!/usr/bin/env python3
"""EXECUTION_CALIBRATION_V1.1 — Mode B scheduler (read-only; never transmits).

Runs the deterministic Mode B sampler once per 15-minute bucket, aligned to
:00/:15/:30/:45 wall clock. Maintains the append-only audit trail and the latest
pointer. It never arms, clears, or calls the submitter.

Usage:
    .venv/Scripts/python.exe scripts/execution_calibration_scheduler.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLER = ROOT / "scripts" / "execution_calibration_modeb.py"
OUT = ROOT / "data" / "execution_calibration" / "modeb_candidates.jsonl"
LATEST = ROOT / "data" / "execution_calibration" / "latest_candidate.json"
BUCKET_SECONDS = 900


def _seen_buckets() -> set:
    if not OUT.exists():
        return set()
    seen = set()
    for line in OUT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            b = json.loads(line).get("timestamp_bucket")
            if b is not None:
                seen.add(b)
        except json.JSONDecodeError:
            continue
    return seen


def _write_latest() -> None:
    if not OUT.exists():
        return
    lines = [l for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
    if lines:
        LATEST.parent.mkdir(parents=True, exist_ok=True)
        LATEST.write_text(lines[-1] + "\n", encoding="utf-8")


def main() -> int:
    seen = _seen_buckets()
    while True:
        now = time.time()
        next_q = (int(now) // BUCKET_SECONDS + 1) * BUCKET_SECONDS
        time.sleep(max(0.0, next_q - now))
        bucket = int(time.time()) // BUCKET_SECONDS
        if bucket in seen:
            continue  # one candidate per bucket: NO-OP on restart replay
        seen.add(bucket)
        subprocess.run([sys.executable, str(SAMPLER)], cwd=ROOT)
        _write_latest()


if __name__ == "__main__":
    raise SystemExit(main())