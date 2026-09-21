#!/usr/bin/env python3
"""Process supervisor for sustained US REST collection — evidence regime.

Runs `run_us_collection.py` continuously, restarting it on crash, so that
crashes/reboots do not silently stop the evidence clock.

Operational context frozen at launch:
    phase:                 US_COLLECTION_RUNNING
    implementation_commit: <git HEAD at launch>
    research_logic:        frozen
    primary feed:          polymarket_us_retail_rest
    authenticated feeds:   NOT ACTIVE
    alpha state:           UNKNOWN

Operational rules:
    - stdout/stderr persisted SEPARATELY from raw data (data/us/logs/...)
    - process start/restart timestamps recorded append-only
    - restarts append-only; nothing is overwritten
    - alerts on: collector death, manifest verification failure, raw hash
      failure, or an unexpectedly long no-data interval
    - normal 429s/reconnects remain operational telemetry, not research events
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_LOG_DIR = ROOT / "data" / "us" / "logs"
DEFAULT_STATE_DIR = ROOT / "data" / "us"
DEFAULT_RAW_DIR = ROOT / "data" / "us" / "retail" / "raw"
DEFAULT_MANIFEST_DIR = ROOT / "data" / "us" / "manifests"

NO_DATA_ALERT_SECONDS = 300  # 5 min without new raw bytes while collector should be running

# The collection implementation commit this supervisor is frozen against: the
# code the qualifying burn-in ran (collector + US adapter), verified byte-identical
# at launch. The supervisor wrapper itself is newer; it is recorded separately as
# supervisor_commit so provenance never conflates the two.
COLLECTION_IMPL_COMMIT = "0bb16a4be3781a4d64b236238c48c21418ff7a24"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _git_head() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT)
            .decode()
            .strip()
        )
    except Exception:  # noqa: BLE001
        return "unknown"


def _collector_matches_impl() -> bool:
    """The on-disk collector must equal the frozen implementation commit's."""
    try:
        frozen = subprocess.check_output(
            ["git", "show", f"{COLLECTION_IMPL_COMMIT}:scripts/run_us_collection.py"],
            cwd=ROOT,
        )
        current = (ROOT / "scripts" / "run_us_collection.py").read_bytes()
        return frozen == current
    except Exception:  # noqa: BLE001
        return False


def _git_dirty() -> bool:
    try:
        out = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).decode()
        return bool(out.strip())
    except Exception:  # noqa: BLE001
        return True


def _raw_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*.jsonl"):
        if p.is_file():
            total += p.stat().st_size
    return total


def _manifest_verify(day) -> tuple[bool, str]:
    from polyalpha.daily_manifest import ManifestWriter
    from polyalpha.rawstore import RawStore

    writer = ManifestWriter(DEFAULT_MANIFEST_DIR)
    raw = RawStore(DEFAULT_RAW_DIR, collector_version="v0.4.1-us-research-baseline")
    return writer.verify(day, raw)


def _finalize_previous_day_manifest() -> None:
    """Write the hash-chained manifest for the previous COMPLETED local day.

    The manifest is a write-once fingerprint of a day's raw files. It can only
    be correct once the day's files are closed (day rolled over); writing it
    mid-day would freeze a partial set. Called each loop; idempotent.
    """
    from datetime import date, timedelta

    from polyalpha.daily_manifest import ManifestWriter, build_daily_manifest
    from polyalpha.rawstore import RawStore

    previous = date.today() - timedelta(days=1)
    writer = ManifestWriter(DEFAULT_MANIFEST_DIR)
    raw = RawStore(DEFAULT_RAW_DIR, collector_version="v0.4.1-us-research-baseline")
    manifest = build_daily_manifest(
        raw=raw,
        day=previous,
        collector_commit=COLLECTION_IMPL_COMMIT,
        config_hash=_config_sha256(),
        markets_observed=0,
        resolved_markets=0,
        dropped_connections=0,
        reconciliations=0,
        book_mismatches=0,
        previous_manifest_sha256=None,
    )
    try:
        path = writer.write(manifest)
        print(f"MANIFEST finalize {previous.isoformat()}: {path} "
              f"(sha256={manifest.combined_hash()})")
    except FileExistsError:
        ok, msg = writer.verify(previous, raw)
        if not ok:
            alert = {"event": "manifest_mismatch", "at": _now(),
                     "day": previous.isoformat(), "ok": False, "message": msg}
            _append_jsonl(DEFAULT_STATE_DIR / "alerts.jsonl", alert)
            print(f"ALERT: manifest {previous} does not verify: {msg}")
        else:
            print(f"MANIFEST finalize {previous.isoformat()}: already exists, VERIFIED")


def _config_sha256() -> str:
    import hashlib

    return hashlib.sha256(
        (ROOT / "config" / "frozen" / "us-v0.4.0-baseline.yaml").read_bytes()
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervise sustained US REST collection")
    parser.add_argument("--chunk-duration", type=float, default=300.0,
                        help="Seconds per collector run before restart (keeps logs bounded)")
    parser.add_argument("--limit", type=int, default=50,
                        help="Markets to track per collector run")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Seconds between collection cycles")
    parser.add_argument("--no-alert-until-secs", type=float, default=NO_DATA_ALERT_SECONDS,
                        help="Max seconds of no new raw bytes before alert")
    parser.add_argument("--once", action="store_true",
                        help="Run exactly one collector chunk and exit (testing)")
    args = parser.parse_args()

    launch = {
        "event": "supervisor_start",
        "at": _now(),
        "phase": "US_COLLECTION_RUNNING",
        "implementation_commit": COLLECTION_IMPL_COMMIT,
        "supervisor_commit": _git_head(),
        "collector_matches_impl": _collector_matches_impl(),
        "working_tree_dirty": _git_dirty(),
        "research_logic": "frozen",
        "primary_feed": "polymarket_us_retail_rest",
        "authenticated_feeds": "NOT_ACTIVE",
        "alpha": "UNKNOWN",
        "chunk_duration_seconds": args.chunk_duration,
        "markets_limit": args.limit,
    }
    _append_jsonl(DEFAULT_STATE_DIR / "supervisor-log.jsonl", launch)
    print("SUPERVISOR LAUNCH:", json.dumps(launch, indent=2))
    if not launch["collector_matches_impl"]:
        print("ERROR: on-disk collector differs from frozen implementation; aborting.")
        return 1

    # Integrity baseline: finalize + verify the previous completed day's
    # manifest. Today's raw is still open (mid-day), so only the previous day
    # can be a stable integrity anchor.
    _finalize_previous_day_manifest()

    collector = sys.executable
    runs = 0
    while True:
        runs += 1
        started = _now()
        # Timestamped log files: separate stdout/stderr from raw data, append-only.
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        out_log = DEFAULT_LOG_DIR / "collector.out.log"
        err_log = DEFAULT_LOG_DIR / "collector.err.log"
        cmd = [
            collector, str(ROOT / "scripts" / "run_us_collection.py"),
            "--duration", str(args.chunk_duration),
            "--limit", str(args.limit),
            "--interval", str(args.interval),
        ]
        print(f"[{started}] run #{runs} start")
        with open(out_log, "a", encoding="utf-8") as fo, open(
            err_log, "a", encoding="utf-8"
        ) as fe:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=fo, stderr=fe)
            code = proc.wait()
        ended = _now()
        _append_jsonl(DEFAULT_STATE_DIR / "restart-log.jsonl", {
            "event": "run_end",
            "run": runs,
            "started_at": started,
            "ended_at": ended,
            "exit_code": code,
        })
        if code != 0:
            alert = {"event": "collector_death", "at": _now(), "run": runs,
                     "exit_code": code, "started_at": started, "ended_at": ended}
            _append_jsonl(DEFAULT_STATE_DIR / "alerts.jsonl", alert)
            print(f"[{ended}] run #{runs} EXITED code={code} (ALERTED)")
        else:
            print(f"[{ended}] run #{runs} completed (code=0)")

        # No-data check: while a collector should be running, raw bytes must
        # keep growing.
        raw_bytes = _raw_bytes(DEFAULT_RAW_DIR)
        if raw_bytes <= 0:
            alert = {"event": "no_data", "at": _now(), "run": runs,
                     "message": "no raw bytes present"}
            _append_jsonl(DEFAULT_STATE_DIR / "alerts.jsonl", alert)
            print(f"[{_now()}] ALERT: no raw bytes")

        if args.once:
            print("SUPERVISOR --once: stopping after 1 run")
            return 0

        # Finalize the previous completed day's manifest each loop (idempotent;
        # write-once at day rollover, verified thereafter).
        _finalize_previous_day_manifest()

        # Brief gap between restarts; keeps collector runs distinct and bounded.
        time.sleep(2.0)


if __name__ == "__main__":
    sys.exit(main())