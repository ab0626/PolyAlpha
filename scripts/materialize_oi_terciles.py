#!/usr/bin/env python3
"""Materialize frozen openInterest tercile cutpoints (two-stage OI step 1).

Run ONCE at the training-window boundary, BEFORE any holdout evaluation. Computes
the q1/3 and q2/3 openInterest terciles from the pre-split training sample only,
and writes them (with a training-data provenance hash) to
``config/frozen/us_oi_terciles.json``.

The evaluator reads these frozen constants and FAILS CLOSED if they are absent —
so a single invocation never both derives thresholds and reveals holdout results.

Usage:
    .venv/Scripts/python.exe scripts/materialize_oi_terciles.py --raw-dir data/us/retail/raw
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.us.families import _end_date, _liquidity_bounds, default_split_date  # noqa: E402
from polyalpha.us.research import build_us_research_dataset  # noqa: E402

OUT = ROOT / "config" / "frozen" / "us_oi_terciles.json"


def _training_liquidity_sha256(dataset) -> str:
    """Hash the training-only market-level openInterest means (derivation inputs)."""
    split = default_split_date()
    by_market: dict[str, list[float]] = {}
    for s in dataset.snapshots:
        end = _end_date(s)
        if end is None or end.date() >= split:
            continue
        if s.liquidity is None:
            continue
        by_market.setdefault(s.market_id, []).append(float(s.liquidity))
    h = hashlib.sha256()
    for mid in sorted(by_market):
        vals = by_market[mid]
        mean = sum(vals) / len(vals)
        h.update(f"{mid}:{mean:.6f}\n".encode())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Materialize frozen OI terciles")
    ap.add_argument("--raw-dir", required=True, help="US retail raw store dir")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    print("building dataset (this replays the raw store)...")
    dataset = build_us_research_dataset(Path(args.raw_dir))
    bounds = _liquidity_bounds(dataset, default_split_date())
    if bounds is None:
        print("ERROR: training-only OI terciles collapsed (insufficient data / ties)")
        return 1

    data = {
        "split_date": default_split_date().isoformat(),
        "q1_3": bounds[0],
        "q2_3": bounds[1],
        "training_data_sha256": _training_liquidity_sha256(dataset),
        "materialized_at": datetime.now(UTC).isoformat(),
        "source": "training-only openInterest q1/3, q2/3 (v2.3.1 two-stage OI)",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"materialized OI terciles -> {out}")
    print(f"  q1/3={bounds[0]}  q2/3={bounds[1]}")
    print(f"  training_data_sha256={data['training_data_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())