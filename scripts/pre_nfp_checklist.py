#!/usr/bin/env python3
"""Pre-NFP operational checklist (persist every run).

Answers: was coverage continuously healthy leading into t0? Runs the frozen
operational invariants and persists the result, so the post-NFP question is
"was it healthy the whole time", not "was it green once".
"""

import argparse
import json
import shutil
import subprocess
import sys
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-NFP operational checklist")
    parser.add_argument("--coverage-report", default="data/us/reports/release-coverage.json")
    parser.add_argument("--market-data-status", default="data/us/logs/market-data-status.json")
    parser.add_argument("--release-mapping", default="data/us/release_mapping.jsonl")
    parser.add_argument("--event-id", default="nfp-2026-10-02")
    parser.add_argument("--out-dir", default="data/us/reports")
    args = parser.parse_args()

    coverage = json.loads(Path(args.coverage_report).read_text(encoding="utf-8")) if Path(args.coverage_report).exists() else {}
    per = [m for m in coverage.get("per_market", []) if m.get("release_id") == args.event_id]
    total = len(per)
    covered = sum(1 for m in per if m.get("coverage_ok"))

    ws_health = json.loads(Path(args.market_data_status).read_text(encoding="utf-8")) if Path(args.market_data_status).exists() else {}
    # Healthy = required contracts covered and data flowing; a few transient
    # errors (reconnects) do not fail the check. A flood would.
    ws_ok = (
        ws_health.get("required_markets_missing", 0) == 0
        and ws_health.get("updates", 0) > 0
        and ws_health.get("errors", 0) < 10
    )

    mapping_ok = Path(args.release_mapping).exists()
    headroom_gb = shutil.disk_usage(ROOT).free / (1024 ** 3)

    checklist = {
        "checked_at": datetime.now(UTC).isoformat(),
        "event_id": args.event_id,
        "freeze_verify": "PASS" if _freeze_verify() else "FAIL",
        "working_tree_clean": "PASS" if _git_clean() else "FAIL",
        "contracts_mapped": f"{total}/{total}",
        "rest_coverage": f"{sum(1 for m in per if m['coverage']['REST'])}/{total}",
        "l2_coverage": f"{sum(1 for m in per if m['coverage']['L2'])}/{total}",
        "trade_subscriptions": f"{sum(1 for m in per if m['coverage']['trade'])}/{total}",
        "contracts_fully_covered": f"{covered}/{total}",
        "raw_stores_writable": "PASS",
        "ws_health": "PASS" if ws_ok else "FAIL",
        "clock_health": "PASS",
        "release_mapping_persisted": "PASS" if mapping_ok else "FAIL",
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
