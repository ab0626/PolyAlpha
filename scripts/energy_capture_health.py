#!/usr/bin/env python3
"""Energy capture health check / watchdog.

Read-only. The ground-truth signal is DATA FRESHNESS, not process existence:

    Health = last_successful_capture_at within --max-stale-minutes
             AND not crash-looping (most recent collector run exited nonzero
             and that exit is recent)

A capture process that is alive but no longer writing snapshots is UNHEALTHY.

Inputs:
    status.json      <- written by energy_capture.py each successful cycle
    restart-log.jsonl <- written by energy_capture_supervisor.py per run end

Optional outputs:
    --log-file     append a timestamped one-line JSON result per check
    --alert-file   append a JSON alert when the system transitions to unhealthy
    --loop-minutes run the check repeatedly every N minutes (watchdog mode)

Usage:
    python scripts/energy_capture_health.py [--max-stale-minutes 5]
    python scripts/energy_capture_health.py --loop-minutes 15 \
        --log-file data/energy/kalshi/health-check.log \
        --alert-file data/energy/kalshi/alerts.jsonl
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = ROOT / "data" / "energy" / "kalshi" / "status.json"
RESTART_LOG = ROOT / "data" / "energy" / "kalshi" / "restart-log.jsonl"

_unhealthy_since: float | None = None


def _toast(title: str, message: str, kind: str = "warning") -> None:
    """Show a Windows desktop toast via a transient NotifyIcon balloon.

    Best-effort: fails silently on non-Windows or headless sessions.
    """
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
    except Exception:  # noqa: BLE001 — notification is best-effort
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
    last_success_at = _parse_iso(status.get("last_successful_capture_at")) if status else None
    last_row_count = status.get("last_row_count") if status else None
    stale_seconds = (now - last_success_at).total_seconds() if last_success_at else None
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
        "last_successful_capture_at": (
            last_success_at.isoformat() if last_success_at else None
        ),
        "last_row_count": last_row_count,
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
            "event": "energy_capture_unhealthy",
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
                "PolyAlpha energy capture DOWN",
                f"energy ladder capture unhealthy (crash_loop={crash_loop}, "
                f"stale={stale_seconds}s)",
                kind="warning",
            )
        elif healthy and was_healthy is False:
            down_seconds = time.time() - (_unhealthy_since or time.time())
            _toast(
                "PolyAlpha energy capture recovered",
                f"energy ladder capture healthy again (down ~{down_seconds / 60.0:.0f} min)",
                kind="info",
            )
            _unhealthy_since = None

    print(json.dumps(report, indent=2, sort_keys=True))
    return healthy


def main() -> int:
    parser = argparse.ArgumentParser(description="Energy capture health check")
    parser.add_argument("--status-file", default=str(STATUS_FILE))
    parser.add_argument("--restart-log", default=str(RESTART_LOG))
    parser.add_argument("--max-stale-minutes", type=float, default=5.0,
                        help="unhealthy if no successful capture within this many "
                             "minutes (default 5, i.e. several 30s intervals)")
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