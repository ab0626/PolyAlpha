"""Prospective Kalshi energy ladder collector.

Records every active energy ladder snapshot to the immutable energy raw lineage:

    data/energy/kalshi/ladders/YYYY-MM-DD.jsonl

One row per active market per cycle, with the venue's own timestamps and a local
receipt clock. Run continuously to accumulate forward microstructure evidence:

    .venv/Scripts/python.exe scripts/energy_capture.py --interval 30

A capture gap is a permanent hole in the forward sample, so this should be kept
running (see the launcher notes in AGENTS.md for how the existing collectors are
started at login).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.capture import (  # noqa: E402
    DEFAULT_SERIES,
    append_jsonl,
    ladder_raw_dir,
    snapshot_energy_ladders,
)
from polyalpha.energy.kalshi import KalshiPublicClient  # noqa: E402


def _write_status(path: str | Path | None, started: datetime, rows: int, interval: float) -> None:
    """Atomically persist the freshness heartbeat used by the health watchdog."""
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "last_successful_capture_at": started.isoformat(),
        "last_row_count": rows,
        "interval_seconds": interval,
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


def main() -> int:
    parser = argparse.ArgumentParser(description="Kalshi energy ladder collector")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between cycles")
    parser.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    parser.add_argument("--series", nargs="*", default=list(DEFAULT_SERIES))
    parser.add_argument("--root", default="data/energy/kalshi/ladders")
    parser.add_argument("--status-file", default=None,
                        help="Path to atomically-written freshness status JSON "
                             "(default: none, for one-shot/bounded runs)")
    args = parser.parse_args()

    client = KalshiPublicClient()
    run = 0
    while args.cycles == 0 or run < args.cycles:
        started = datetime.now(UTC)
        rows = snapshot_energy_ladders(client, tuple(args.series))
        path = ladder_raw_dir(args.root) / f"{started.strftime('%Y-%m-%d')}.jsonl"
        n = append_jsonl(path, rows)
        _write_status(args.status_file, started, len(rows), args.interval)
        print(f"[{started.isoformat()}] captured {len(rows)} active markets -> {path} ({n} bytes)", flush=True)
        run += 1
        if args.cycles != 0 and run >= args.cycles:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())