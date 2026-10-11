#!/usr/bin/env python3
"""ERCOT forecast-vintage capture health check / watchdog.

Read-only. The ground-truth signal is DATA FRESHNESS, not process existence:

    Health = last_successful_poll_at within --max-stale-minutes
             AND not crash-looping (most recent collector run exited nonzero
             and that exit is recent)

A collector that is alive but no longer polling is UNHEALTHY. "No new ERCOT
publication" (a quiet feed) is NOT a failure: it only moves ``last_new_doc_at``,
never ``last_successful_poll_at``.

Inputs:
    status.json      <- written by energy_forecast_collector.py each successful poll
    restart-log.jsonl <- written by energy_forecast_supervisor.py per run end

Usage:
    python scripts/energy_forecast_health.py [--max-stale-minutes 10]
    python scripts/energy_forecast_health.py --loop-minutes 15 \
        --log-file data/energy/ercot/health-check.log \
        --alert-file data/energy/ercot/alerts.jsonl
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = ROOT / "data" / "energy" / "ercot" / "status.json"
RESTART_LOG = ROOT / "data" / "energy" / "ercot" / "restart-log.jsonl"

_unhealthy_since: float | None = None


def _toast(title: str, message: str, kind: str = "warning") -> None:
    if sys.platform != "win32":
        return
    if kind == "info":
        icon, tip = "Information", "Info"
    else:
        icon, tip = "Warning", "Warning"
    safe_title = title.replace("'", "`'")
    safe_message = message.replace("'", "`'")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        f"$n.Icon = [System.Drawing.SystemIcons]::{icon}; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(10000, '{safe_title}', '{safe_message}', "
        f"[System.Windows.Forms.ToolTipIcon]::{tip}); "
        "Start-Sleep -Seconds 12; $n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        pass


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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
    now = datetime.now(UTC)
    max_stale_seconds = args.max_stale_minutes * 60

    status = _read_json(Path(args.status_file))
    last_poll_at = _parse_iso(status.get("last_successful_poll_at")) if status else None
    last_new_doc_at = status.get("last_new_doc_at") if status else None
    total_docs = status.get("total_docs_captured") if status else None
    stale_seconds = (now - last_poll_at).total_seconds() if last_poll_at else None
    staleness_ok = stale_seconds is not None and stale_seconds <= max_stale_seconds

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
        "last_successful_poll_at": last_poll_at.isoformat() if last_poll_at else None,
        "last_new_doc_at": last_new_doc_at,
        "total_docs_captured": total_docs,
        "last_collector_exit_code": last_exit,
        "max_stale_minutes": args.max_stale_minutes,
    }

    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now(UTC).isoformat(), **report}
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    if not healthy and args.alert_file and (was_healthy is None or was_healthy):
        alert_path = Path(args.alert_file)
        alert_path.parent.mkdir(parents=True, exist_ok=True)
        alert = {
            "event": "energy_forecast_capture_unhealthy",
            "at": datetime.now(UTC).isoformat(),
            **{k: v for k, v in report.items() if k != "healthy"},
        }
        with open(alert_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert, sort_keys=True) + "\n")

    global _unhealthy_since
    if args.toast:
        if not healthy and (was_healthy is None or was_healthy):
            _unhealthy_since = time.time()
            _toast(
                "PolyAlpha ERCOT vintage capture DOWN",
                f"forecast-vintage collector unhealthy (crash_loop={crash_loop}, "
                f"stale={stale_seconds}s)",
                kind="warning",
            )
        elif healthy and was_healthy is False:
            down_seconds = time.time() - (_unhealthy_since or time.time())
            _toast(
                "PolyAlpha ERCOT vintage capture recovered",
                f"forecast-vintage collector healthy again (down ~{down_seconds / 60.0:.0f} min)",
                kind="info",
            )
            _unhealthy_since = None

    print(json.dumps(report, indent=2, sort_keys=True))
    return healthy


def main() -> int:
    parser = argparse.ArgumentParser(description="ERCOT forecast-vintage health check")
    parser.add_argument("--status-file", default=str(STATUS_FILE))
    parser.add_argument("--restart-log", default=str(RESTART_LOG))
    parser.add_argument("--max-stale-minutes", type=float, default=10.0,
                        help="unhealthy if no successful poll within this many "
                             "minutes (default 10; cadence is 5 min)")
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--alert-file", default=None)
    parser.add_argument("--toast", action="store_true",
                        help="Show a Windows desktop toast on transition to "
                             "unhealthy and again on recovery (best-effort)")
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