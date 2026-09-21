#!/usr/bin/env python3
"""Lightweight US collection health check / watchdog.

Read-only: inspects the raw store and the supervisor/restart logs, prints a
JSON health summary, and exits 0 (healthy) or 1 (unhealthy).

Ground-truth signal: is new raw data arriving?
  - staleness: no raw file modified within --max-stale-minutes => unhealthy
  - crash-loop: the most recent collector run ended nonzero AND is recent
                (the supervisor is retrying a failing collector)

Optional outputs:
  --log-file     append a timestamped one-line JSON result per check
  --alert-file   append a JSON alert when the system transitions to unhealthy
  --loop-minutes run the check repeatedly every N minutes (watchdog mode)

Usage:
    python scripts/us_collection_health.py [--max-stale-minutes 15]
    python scripts/us_collection_health.py --loop-minutes 15 \
        --log-file data/us/logs/health-check.log \
        --alert-file data/us/alerts.jsonl
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "us" / "retail" / "raw"
SUPERVISOR_LOG = ROOT / "data" / "us" / "supervisor-log.jsonl"
RESTART_LOG = ROOT / "data" / "us" / "restart-log.jsonl"


def _newest_mtime(root: Path) -> float:
    newest = 0.0
    if not root.exists():
        return newest
    for path in root.rglob("*"):
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return newest


def _last_jsonl(path: Path) -> dict | None:
    if not path.exists():
        return None
    last = ""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _run_check(args: argparse.Namespace, was_healthy: bool | None) -> bool:
    raw_dir = Path(args.raw_dir)
    max_stale_seconds = args.max_stale_minutes * 60
    now = datetime.now(UTC)

    newest = _newest_mtime(raw_dir)
    stale_seconds = (time.time() - newest) if newest else None
    staleness_ok = stale_seconds is not None and stale_seconds <= max_stale_seconds

    supervisor = _last_jsonl(Path(args.supervisor_log))
    last_run = _last_jsonl(Path(args.restart_log))

    last_exit = last_run.get("exit_code") if last_run else None
    last_end_at = _parse_iso(last_run.get("ended_at")) if last_run else None
    last_run_recent = (
        last_end_at is not None
        and (now - last_end_at).total_seconds() <= max_stale_seconds
    )
    crash_loop = bool(last_exit is not None and last_exit != 0 and last_run_recent)

    healthy = bool(staleness_ok) and not crash_loop

    report = {
        "healthy": healthy,
        "staleness_ok": bool(staleness_ok),
        "crash_loop": crash_loop,
        "stale_seconds": round(stale_seconds, 1) if stale_seconds is not None else None,
        "newest_raw_file_mtime": (
            datetime.fromtimestamp(newest, tz=UTC).isoformat() if newest else None
        ),
        "supervisor_phase": (supervisor or {}).get("phase"),
        "supervisor_last_started_at": (supervisor or {}).get("at"),
        "last_collector_exit_code": last_exit,
        "raw_dir": str(raw_dir),
    }

    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now(UTC).isoformat(), **report}
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    # Alert only on transition into unhealthy (first check, or after a healthy
    # period) to avoid spamming the alert stream while a failure persists.
    if not healthy and args.alert_file and (was_healthy is None or was_healthy):
        alert_path = Path(args.alert_file)
        alert_path.parent.mkdir(parents=True, exist_ok=True)
        alert = {
            "event": "collection_health_unhealthy",
            "at": datetime.now(UTC).isoformat(),
            **{k: v for k, v in report.items() if k != "healthy"},
        }
        with open(alert_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert, sort_keys=True) + "\n")

    print(json.dumps(report, indent=2, sort_keys=True))
    return healthy


def main() -> int:
    parser = argparse.ArgumentParser(description="US collection health check")
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--supervisor-log", default=str(SUPERVISOR_LOG))
    parser.add_argument("--restart-log", default=str(RESTART_LOG))
    parser.add_argument("--max-stale-minutes", type=float, default=15.0)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--alert-file", default=None)
    parser.add_argument("--loop-minutes", type=float, default=0.0)
    args = parser.parse_args()

    if args.loop_minutes and args.loop_minutes > 0:
        was_healthy: bool | None = None
        while True:
            try:
                was_healthy = _run_check(args, was_healthy)
            except Exception as error:  # noqa: BLE001
                print(f"health check error: {error}", file=sys.stderr)
            time.sleep(args.loop_minutes * 60)
    else:
        healthy = _run_check(args, None)
        return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
