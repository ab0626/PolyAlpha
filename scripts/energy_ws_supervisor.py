#!/usr/bin/env python3
"""Authenticated Kalshi WebSocket collector supervisor — durable, self-restarting.

Runs ``energy_ws_collector.py`` continuously and restarts it on crash. Lifecycle
events are append-only (nothing overwritten):

    data/energy/kalshi/ws/supervisor-log.jsonl   (one line per supervisor start)
    data/energy/kalshi/ws/restart-log.jsonl      (started_at / ended_at / exit_code / restart_count)
    data/energy/kalshi/ws/status.json            (freshness heartbeat, written by the collector)
    data/energy/kalshi/ws/alerts.jsonl           (death events)

Health is DATA FRESHNESS, not process existence: see ``energy_ws_health.py``.

Usage:
    .venv/Scripts/python.exe scripts/energy_ws_supervisor.py \
        --env-file C:\\Users\\adith\\powermarkets.env
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STATE_DIR = ROOT / "data" / "energy" / "kalshi" / "ws"
STATUS_FILE = STATE_DIR / "status.json"
SUPERVISOR_LOG = STATE_DIR / "supervisor-log.jsonl"
RESTART_LOG = STATE_DIR / "restart-log.jsonl"
ALERTS_LOG = STATE_DIR / "alerts.jsonl"
COLLECTOR_SCRIPT = ROOT / "scripts" / "energy_ws_collector.py"

DEFAULT_ENV_FILE = r"C:\Users\adith\powermarkets.env"
RESTART_BACKOFF_SECONDS = 5.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervise the Kalshi WS collector")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--series", nargs="*", default=None,
                        help="series tickers (default: all energy series)")
    parser.add_argument("--root", default="data/energy/kalshi/ws")
    args = parser.parse_args()

    _append_jsonl(SUPERVISOR_LOG, {
        "event": "supervisor_start",
        "at": _now(),
        "phase": "KALSHI_WS_COLLECTING",
        "env_file": args.env_file,
        "root": args.root,
    })

    collector = sys.executable
    runs = 0
    while True:
        runs += 1
        started = _now()
        out_log = STATE_DIR / "collector.out.log"
        err_log = STATE_DIR / "collector.err.log"
        cmd = [
            collector, str(COLLECTOR_SCRIPT),
            "--env-file", args.env_file,
            "--root", args.root,
            "--status-file", str(STATUS_FILE),
        ]
        if args.series:
            cmd += ["--series", *args.series]
        print(f"[{started}] kalshi-ws run #{runs} start", flush=True)

        with open(out_log, "a", encoding="utf-8") as fo, open(
            err_log, "a", encoding="utf-8"
        ) as fe:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=fo, stderr=fe)
            code = proc.wait()

        ended = _now()
        _append_jsonl(RESTART_LOG, {
            "event": "run_end",
            "run": runs,
            "started_at": started,
            "ended_at": ended,
            "exit_code": code,
            "restart_count": runs - 1,
        })

        if code != 0:
            _append_jsonl(ALERTS_LOG, {
                "event": "collector_death",
                "at": _now(),
                "run": runs,
                "exit_code": code,
                "started_at": started,
                "ended_at": ended,
            })
            print(f"[{ended}] run #{runs} EXITED code={code} (ALERTED)", flush=True)
        else:
            print(f"[{ended}] run #{runs} exited code=0; restarting", flush=True)

        time.sleep(RESTART_BACKOFF_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())