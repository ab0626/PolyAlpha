#!/usr/bin/env python3
"""US collection gap watchdog — active recovery daemon (with honest gap journal).

Monitors the US retail collector's *supervisor* process and the raw-store data
heartbeat. When collection stops it:

  1. records the outage in its OWN append-only journal (NOT the canonical
     research gap ledger), with gap start = last known healthy heartbeat and
     gap end = first fresh heartbeat after recovery;
  2. alerts immediately;
  3. restarts the collector with bounded backoff (no infinite restart storm).

It never edits ``config/us_known_gaps.json``. ``scripts/reconcile_us_gaps.py``
turns this journal into candidate ledger additions for human review.

Usage:
    python scripts/us_gap_watchdog.py                     # run forever
    python scripts/us_gap_watchdog.py --once              # one check, then exit
    python scripts/us_gap_watchdog.py --dry-run --once    # no process side effects
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.notify import toast  # noqa: E402
from polyalpha.us.gap_watchdog import (  # noqa: E402
    OUTAGE_OPEN,
    RECOVERING,
    Action,
    GapJournal,
    Reading,
    StateMachine,
    StateStore,
    coverage_snapshot,
)

DEFAULT_RAW_DIR = ROOT / "data" / "us" / "retail" / "raw"
DEFAULT_JOURNAL_DIR = ROOT / "data" / "us" / "gap_watchdog"
DEFAULT_EVENTS = DEFAULT_JOURNAL_DIR / "events.jsonl"
DEFAULT_STATE = DEFAULT_JOURNAL_DIR / "state.json"
DEFAULT_SUPERVISOR_LOG = ROOT / "data" / "us" / "logs"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ts_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


# -- process manager ---------------------------------------------------------


def _tasklist_has_pid(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f'"{pid}"' in out


def _find_supervisor_pid() -> int | None:
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*us_collection_supervisor.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


class ProcessManager:
    """Launch/adopt/kill the US collection supervisor, guarding against dupes."""

    def __init__(self, cmd: list[str], dry_run: bool = False) -> None:
        self.cmd = cmd
        self.dry_run = dry_run
        self.pid: int | None = None

    def _adopt(self) -> None:
        if self.dry_run:
            return
        # Always (re)assign, so a dead PID is cleared rather than kept stale.
        self.pid = _find_supervisor_pid()

    def is_alive(self) -> bool:
        if self.dry_run:
            return True
        if self.pid is None:
            self._adopt()
        if self.pid is None:
            return False
        return _tasklist_has_pid(self.pid)

    def launch(self) -> int | None:
        if self.dry_run:
            self.pid = self.pid or -1
            return self.pid
        # Adopt an already-running supervisor instead of launching a duplicate.
        self._adopt()
        # Only adopt a pid that is genuinely still alive; a stale pid (the
        # process died) must fall through to a real launch.
        if self.pid is not None and _tasklist_has_pid(self.pid):
            return self.pid
        self.pid = None
        (DEFAULT_SUPERVISOR_LOG).mkdir(parents=True, exist_ok=True)
        out_log = open(DEFAULT_SUPERVISOR_LOG / "watchdog-supervisor.out.log", "a", encoding="utf-8")
        err_log = open(DEFAULT_SUPERVISOR_LOG / "watchdog-supervisor.err.log", "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(self.cmd, cwd=ROOT, stdout=out_log, stderr=err_log)
        finally:
            out_log.close()
            err_log.close()
        self.pid = proc.pid
        return proc.pid

    def kill(self) -> None:
        if self.dry_run or self.pid is None:
            self.pid = None
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/F", "/T"],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.pid = None


# -- heartbeat ---------------------------------------------------------------


def _newest_mtime_in(dir_path: Path) -> float | None:
    newest: float | None = None
    if not dir_path.is_dir():
        return None
    for p in dir_path.rglob("*"):
        if p.is_file():
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return newest


def _newest_recent_mtime(raw_dir: Path, now: datetime, lookback_days: int = 2) -> float | None:
    newest: float | None = None
    for i in range(lookback_days):
        day = now - timedelta(days=i)
        d = raw_dir / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        m = _newest_mtime_in(d)
        if m is not None and (newest is None or m > newest):
            newest = m
    return newest


# -- rendering ---------------------------------------------------------------


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "none"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _render_coverage(machine: StateMachine, now_ts: float) -> str:
    open_gap = None
    if machine.state in (OUTAGE_OPEN, RECOVERING) and machine.outage_start is not None:
        open_gap = (
            datetime.fromtimestamp(machine.outage_start, tz=UTC),
            datetime.fromtimestamp(now_ts, tz=UTC),
        )
    snap = coverage_snapshot(datetime.fromtimestamp(now_ts, tz=UTC), open_gap)

    def _pct(x: float | None) -> str:
        return "unknown" if x is None else f"{x * 100:.2f}%"

    return (
        f"official_coverage:    {_pct(snap['official_coverage'])}\n"
        f"provisional_coverage: {_pct(snap['provisional_coverage'])}\n"
        f"open_gap:             {_fmt_duration(snap['open_gap_seconds'])}"
    )


# -- main --------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="US collection gap watchdog")
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    ap.add_argument("--events-file", default=str(DEFAULT_EVENTS))
    ap.add_argument("--state-file", default=str(DEFAULT_STATE))
    ap.add_argument("--check-interval", type=float, default=30.0,
                    help="Seconds between checks (default 30)")
    ap.add_argument("--stale-seconds", type=float, default=900.0,
                    help="Heartbeat staleness threshold (default 900)")
    ap.add_argument("--warmup-grace-seconds", type=float, default=600.0)
    ap.add_argument("--backoff-initial", type=float, default=30.0)
    ap.add_argument("--backoff-factor", type=float, default=2.0)
    ap.add_argument("--backoff-max", type=float, default=600.0)
    ap.add_argument("--max-restart-attempts", type=int, default=6)
    ap.add_argument("--chunk-duration", type=float, default=300.0)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--toast", action="store_true",
                    help="Show Windows desktop toasts on outage/recovery")
    ap.add_argument("--dry-run", action="store_true",
                    help="Never launch/kill processes (stub process manager)")
    ap.add_argument("--once", action="store_true",
                    help="Run a single check and exit")
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)
    journal = GapJournal(args.events_file)
    state_store = StateStore(args.state_file)

    cfg = {
        "stale_seconds": args.stale_seconds,
        "warmup_grace_seconds": args.warmup_grace_seconds,
        "backoff_initial": args.backoff_initial,
        "backoff_factor": args.backoff_factor,
        "backoff_max": args.backoff_max,
        "max_restart_attempts": args.max_restart_attempts,
    }

    # Full one-time scan for the baseline (captures the last write before any
    # pre-watchdog downtime such as a reboot).
    disk_baseline = _newest_mtime_in(raw_dir)

    stored = state_store.load()
    machine = StateMachine(**cfg)
    machine.last_healthy_heartbeat = disk_baseline
    if stored and stored.get("state") in (OUTAGE_OPEN, RECOVERING):
        # Resume an in-progress outage across watchdog restarts.
        machine = StateMachine.from_dict(stored, **cfg)
        if machine.outage_start is None:
            machine.outage_start = disk_baseline

    supervisor_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "us_collection_supervisor.py"),
        "--chunk-duration", str(args.chunk_duration),
        "--limit", str(args.limit),
        "--interval", str(args.interval),
    ]
    pm = ProcessManager(supervisor_cmd, dry_run=args.dry_run)

    journal.append({
        "event": "watchdog_start",
        "state": machine.state,
        "disk_baseline": _ts_iso(disk_baseline),
        "resumed": bool(stored and stored.get("state") in (OUTAGE_OPEN, RECOVERING)),
    })

    while True:
        now_ts = time.time()
        now_dt = datetime.fromtimestamp(now_ts, tz=UTC)
        heartbeat_ts = _newest_recent_mtime(raw_dir, now_dt)
        reading = Reading(
            process_alive=pm.is_alive(),
            heartbeat_ts=heartbeat_ts,
            now=now_ts,
        )

        for action in machine.step(reading):
            _execute(action, journal, pm, args.toast)

        state_store.save(machine.to_dict())
        print(f"[{_now_iso()}] state={machine.state}", flush=True)
        print(_render_coverage(machine, now_ts), flush=True)

        if args.once:
            return 0
        time.sleep(args.check_interval)


def _execute(action: Action, journal: GapJournal, pm: ProcessManager, do_toast: bool) -> None:
    kind = action.kind
    if kind == "launch_process":
        pid = pm.launch()
        journal.append({
            "event": "restart",
            "attempt": action.payload.get("attempt"),
            "launched_pid": pid,
        })
    elif kind == "kill_process":
        pm.kill()
        journal.append({"event": "kill"})
    elif kind == "outage_open":
        journal.append({
            "event": "outage_open",
            "gap_start": _ts_iso(action.payload.get("gap_start")),
            "reason": action.payload.get("reason"),
        })
        if do_toast:
            toast("PolyAlpha collector DOWN", "US retail collector outage opened", kind="warning")
    elif kind == "outage_close":
        journal.append({
            "event": "outage_close",
            "gap_start": _ts_iso(action.payload.get("gap_start")),
            "gap_end": _ts_iso(action.payload.get("gap_end")),
        })
        if do_toast:
            toast("PolyAlpha collector recovered", "US retail collector healthy again", kind="info")
    elif kind == "recovery_failed":
        journal.append({
            "event": "recovery_failed",
            "attempt": action.payload.get("attempt"),
            "reason": action.payload.get("reason"),
        })
        if do_toast:
            toast(
                "PolyAlpha recovery failed",
                f"attempt {action.payload.get('attempt')} ({action.payload.get('reason')})",
                kind="warning",
            )


if __name__ == "__main__":
    sys.exit(main())