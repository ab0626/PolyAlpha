#!/usr/bin/env python3
"""Continuous ERCOT GEN-533-UI intraday actual-load collector.

Fetches the public "Actual Loads of Forecast Zones" display (EMIL GEN-533-UI)
every N seconds and appends a snapshot to
``data/energy/ercot/intraday_actuals.jsonl`` (append-only). Each snapshot records
the display's last-update timestamp, the operating day, the number of hourly rows,
and the running max TOTAL for that day — the intraday realized path the daily
NP6-346 settlement report does not provide at trade time.

A freshness heartbeat is written to ``data/energy/ercot/intraday_status.json``.
This is a separate lineage from the frozen NP6-346 ``actuals.jsonl``.

Usage:
    .venv/Scripts/python.exe scripts/energy_intraday_collector.py --interval 900
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

from polyalpha.energy.intraday_actuals import fetch_intraday_actuals, running_max  # noqa: E402

DEFAULT_ROOT = ROOT / "data" / "energy" / "ercot"
DEFAULT_INTERVAL = 900.0  # seconds; display updates hourly at :20


def _load_last_update(path: Path) -> str | None:
    if not path.exists():
        return None
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("display_last_update"):
            last = d["display_last_update"]
    return last


def main() -> int:
    ap = argparse.ArgumentParser(description="Continuous ERCOT intraday actual-load collector")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    root = Path(args.root)
    data_path = root / "intraday_actuals.jsonl"
    status_path = root / "intraday_status.json"

    status = {
        "started_at": datetime.now(UTC).isoformat(),
        "interval_seconds": args.interval,
        "last_successful_fetch_at": None,
        "last_new_snapshot_at": None,
        "last_error": None,
        "last_error_at": None,
    }

    def write_status() -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, sort_keys=True, default=str) + "\n", encoding="utf-8")
        tmp.replace(status_path)

    write_status()

    run = 0
    while args.cycles == 0 or run < args.cycles:
        try:
            snap = fetch_intraday_actuals()
            if snap is None:
                status["last_error"] = "fetch/parse failed (fail-closed)"
                status["last_error_at"] = datetime.now(UTC).isoformat()
                write_status()
                print(f"[{datetime.now(UTC).isoformat()}] ERROR fetch/parse failed", file=sys.stderr, flush=True)
            else:
                status["last_successful_fetch_at"] = datetime.now(UTC).isoformat()
                days = sorted({d for (d, _h, _t) in snap.rows})
                day = days[-1] if days else None
                rmax = running_max(snap.rows, day) if day else None
                last_update = snap.last_update.isoformat() if snap.last_update else None
                prev = _load_last_update(data_path)
                if last_update is not None and last_update != prev:
                    row = {
                        "captured_at": datetime.now(UTC).isoformat(),
                        "display_last_update": last_update,
                        "operating_day": day,
                        "running_max_mw": str(rmax) if rmax is not None else None,
                        "n_hours": len(snap.rows),
                    }
                    data_path.parent.mkdir(parents=True, exist_ok=True)
                    with data_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row, sort_keys=True) + "\n")
                    status["last_new_snapshot_at"] = row["captured_at"]
                    print(
                        f"[{row['captured_at']}] intraday: day={day} "
                        f"running_max={rmax} hours={len(snap.rows)}",
                        flush=True,
                    )
                else:
                    print(
                        f"[{datetime.now(UTC).isoformat()}] intraday unchanged "
                        f"(last_update={last_update})",
                        flush=True,
                    )
                status["last_error"] = None
                status["last_error_at"] = None
            write_status()
        except Exception as exc:  # noqa: BLE001 — durable: log and retry
            status["last_error"] = f"{type(exc).__name__}: {exc}"
            status["last_error_at"] = datetime.now(UTC).isoformat()
            write_status()
            print(f"[{datetime.now(UTC).isoformat()}] ERROR {status['last_error']}", file=sys.stderr, flush=True)

        run += 1
        if args.cycles != 0 and run >= args.cycles:
            break
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())