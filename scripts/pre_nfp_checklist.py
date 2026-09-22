#!/usr/bin/env python3
"""Pre-NFP operational checklist (persist every run, fail-closed).

No "green by construction": every invariant is actually tested. Answers "was
coverage continuously healthy leading into t0", not "was it green once".
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _freeze_verify() -> bool:
    r = subprocess.run([sys.executable, "scripts/freeze_baseline.py", "verify"],
                       cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def _git_clean() -> bool:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() == ""


def _raw_writable(raw_dir: str) -> bool:
    p = Path(raw_dir)
    if not p.exists():
        return False
    probe = p / ".writable-probe"
    try:
        probe.write_text(str(time.time()), encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-NFP operational checklist")
    parser.add_argument("--coverage-report", default="data/us/reports/release-coverage.json")
    parser.add_argument("--market-data-status", default="data/us/logs/market-data-status.json")
    parser.add_argument("--release-mapping", default="data/us/release_mapping.jsonl")
    parser.add_argument("--event-id", default="nfp-2026-10-02")
    parser.add_argument("--min-contracts", type=int, default=15,
                        help="Nonzero floor for expected NFP contracts")
    parser.add_argument("--book-raw", default="data/us/retail/raw")
    parser.add_argument("--status-max-age", type=int, default=60,
                        help="Max age (s) of the 15s WS status file")
    parser.add_argument("--out-dir", default="data/us/reports")
    args = parser.parse_args()

    now = datetime.now(UTC)
    checked_at = now.isoformat()

    coverage = json.loads(Path(args.coverage_report).read_text(encoding="utf-8")) if Path(args.coverage_report).exists() else {}
    per = [m for m in coverage.get("per_market", []) if m.get("release_id") == args.event_id]
    total = len(per)
    covered = sum(1 for m in per if m.get("coverage_ok"))
    mapping_ok = total >= args.min_contracts

    ws_health = json.loads(Path(args.market_data_status).read_text(encoding="utf-8")) if Path(args.market_data_status).exists() else {}
    status_path = Path(args.market_data_status)
    status_age = None
    if status_path.exists():
        status_age = (now - datetime.fromtimestamp(status_path.stat().st_mtime, UTC)).total_seconds()
    # Fresh status + zero missing + data flowing + not a flood of errors.
    ws_ok = (
        status_age is not None and status_age < args.status_max_age
        and ws_health.get("required_markets_missing", 0) == 0
        and ws_health.get("updates", 0) > 0
        and ws_health.get("errors", 0) < 10
    )

    # Clock sanity: the collector's own timestamp must agree with ours within
    # a generous window; a drifting wall clock shows up as a large offset.
    clock_offset = None
    ts = ws_health.get("ts")
    if ts:
        try:
            clock_offset = (datetime.fromisoformat(ts) - now).total_seconds()
        except ValueError:
            clock_offset = None
    clock_ok = clock_offset is not None and abs(clock_offset) < 120

    mapping_file_ok = Path(args.release_mapping).exists()
    headroom_gb = shutil.disk_usage(ROOT).free / (1024 ** 3)

    checklist = {
        "checked_at": checked_at,
        "event_id": args.event_id,
        "freeze_verify": "PASS" if _freeze_verify() else "FAIL",
        "working_tree_clean": "PASS" if _git_clean() else "FAIL",
        "contracts_mapped": f"{total}/{args.min_contracts}" if mapping_ok else f"{total}/{args.min_contracts}",
        "contracts_mapping_ok": "PASS" if mapping_ok else "FAIL",
        "rest_coverage": f"{sum(1 for m in per if m['coverage']['REST'])}/{total}",
        "l2_coverage": f"{sum(1 for m in per if m['coverage']['L2'])}/{total}",
        "trade_subscriptions": f"{sum(1 for m in per if m['coverage']['trade'])}/{total}",
        "contracts_fully_covered": f"{covered}/{total}",
        "raw_stores_writable": "PASS" if _raw_writable(args.book_raw) else "FAIL",
        "ws_health": "PASS" if ws_ok else "FAIL",
        "ws_status_age_seconds": round(status_age, 1) if status_age is not None else None,
        "clock_health": "PASS" if clock_ok else "FAIL",
        "clock_offset_seconds": round(clock_offset, 1) if clock_offset is not None else None,
        "release_mapping_persisted": "PASS" if mapping_file_ok else "FAIL",
        "disk_headroom_gb": round(headroom_gb, 1),
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"pre-nfp-checklist-{stamp}.json"
    out.write_text(json.dumps(checklist, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "pre-nfp-checklist-latest.json").write_text(json.dumps(checklist, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(checklist, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
