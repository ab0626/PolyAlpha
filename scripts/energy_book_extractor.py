#!/usr/bin/env python3
"""MICROSTRUCTURE-EVIDENCE-V1 extractor + extraction-integrity report (READ-ONLY).

Stage 1 of the frozen contract: deterministic BookState reconstruction, then
horizon labeling (raw + net markout), then a verification report. This does NOT
evaluate H1-H5 — it only proves the machinery is correct before any alpha result
is read.

Usage:
    .venv/Scripts/python.exe scripts/energy_book_extractor.py [--days 1]

Outputs:
    data/energy/kalshi/ws_extract/{day}/bookstates.jsonl      (canonical states)
    data/energy/kalshi/ws_extract/{day}/labels.jsonl          (raw/net markouts)
    stdout: extraction-integrity report
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.book_reconstruction import (  # noqa: E402
    EVIDENCE_PARTITION_BOUNDARY,
    MARKOUT_HORIZONS,
    MARKOUT_TOLERANCE,
    Reconstructor,
)

WS_DIR = ROOT / "data" / "energy" / "kalshi" / "ws"
OUT_DIR = ROOT / "data" / "energy" / "kalshi" / "ws_extract"
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")

BOUNDARY_MS = int(datetime.fromisoformat(EVIDENCE_PARTITION_BOUNDARY).timestamp() * 1000)
TAKER_FEE = Decimal("0.07")


def _partition(venue_ts_ms: int) -> str:
    return "forward" if venue_ts_ms >= BOUNDARY_MS else "dev"


def _label(arr: list[tuple[int, Decimal | None, Decimal | None]],
           ts: list[int], obs: dict) -> dict:
    """Markouts at each horizon using the frozen tolerance rule; MISSING if no
    valid gap-clean future state within tolerance (nearest-by-venue_ts)."""
    t = obs["venue_ts_ms"]
    mid_t = obs["mid"]
    out = {"labels": {}}
    for h in MARKOUT_HORIZONS:
        target = t + int(h * 1000)
        tol = int(MARKOUT_TOLERANCE[h] * 1000)
        i = bisect_left(ts, target)
        best = None
        best_d = tol + 1
        for j in (i - 1, i):
            if 0 <= j < len(ts):
                d = abs(ts[j] - target)
                if d <= tol and d < best_d:
                    best_d = d
                    best = arr[j]
        if best is None or best[1] is None or mid_t is None:
            out["labels"][f"{h}s"] = {"status": "MISSING", "raw_markout": None, "net_markout": None}
            continue
        future_mid = best[1]
        future_spread = best[2]
        raw = future_mid - mid_t
        cost = Decimal("0.5") * (future_spread or Decimal(0)) + TAKER_FEE * future_mid
        net = raw - cost
        out["labels"][f"{h}s"] = {
            "status": "ok",
            "raw_markout": str(raw),
            "net_markout": str(net),
            "future_ts_ms": best[0],
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Microstructure extractor + integrity report")
    ap.add_argument("--days", type=int, default=1, help="most recent N WS day files to process")
    ap.add_argument("--files", nargs="*", default=None, help="explicit WS files")
    ap.add_argument("--write-labels", action="store_true", help="persist labels.jsonl (off by default)")
    args = ap.parse_args()

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        day_files = sorted(p for p in WS_DIR.glob("*.jsonl") if _DAY_RE.match(p.name))
        files = day_files[-args.days:]
    if not files:
        print("no WS day files")
        return 1
    print(f"processing: {[f.name for f in files]}")

    rec = Reconstructor()
    compact: dict[str, list[tuple[int, Decimal | None, Decimal | None]]] = defaultdict(list)
    observations: list[dict] = []
    clusters: set[tuple[str, str]] = set()
    markets: set[str] = set()

    for f in files:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                if '"orderbook_' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bs = rec.apply(row)
                if bs is None:
                    continue
                if bs.gap_clean:
                    compact[bs.ticker].append((bs.venue_ts_ms, bs.mid, bs.spread))
                    day = datetime.fromtimestamp(bs.venue_ts_ms / 1000, UTC).date().isoformat()
                    clusters.add((bs.ticker, day))
                    markets.add(bs.ticker)
                    observations.append({
                        "ticker": bs.ticker,
                        "venue_ts_ms": bs.venue_ts_ms,
                        "mid": bs.mid,
                        "spread": bs.spread,
                        "session_id": bs.session_id,
                        "seq": bs.seq,
                        "frame_id": bs.frame_id,
                        "partition": _partition(bs.venue_ts_ms),
                    })

    ts_by_ticker = {t: [x[0] for x in arr] for t, arr in compact.items()}

    print("\n=== Extraction-integrity report ===")
    rep = rec.stats.report()
    for k, v in rep.items():
        print(f"  {k:22s} {v}")

    # Label coverage (sample every observation is fine for a bounded window).
    horizon_missing = {f"{h}s": 0 for h in MARKOUT_HORIZONS}
    horizon_total = {f"{h}s": 0 for h in MARKOUT_HORIZONS}
    n_obs = len(observations)
    labeled = 0
    for obs in observations:
        lab = _label(compact.get(obs["ticker"], []), ts_by_ticker.get(obs["ticker"], []), obs)
        labeled += 1
        for hk, v in lab["labels"].items():
            horizon_total[hk] += 1
            if v["status"] == "MISSING":
                horizon_missing[hk] += 1

    print(f"\n  observations (gap-clean): {n_obs}")
    print(f"  markets: {len(markets)}")
    print(f"  independent clusters (ticker x day): {len(clusters)}")
    print(f"  partitions:")
    dev = sum(1 for o in observations if o["partition"] == "dev")
    fwd = sum(1 for o in observations if o["partition"] == "forward")
    print(f"    dev:      {dev}")
    print(f"    forward:  {fwd}")

    print("\n  horizon label coverage:")
    for h in MARKOUT_HORIZONS:
        hk = f"{h}s"
        tot = horizon_total[hk]
        miss = horizon_missing[hk]
        cov = 1 - miss / tot if tot else None
        print(f"    {hk:>8s}  coverage={cov if cov is None else round(cov,4)}  missing={miss}/{tot}")

    if args.write_labels:
        day = files[0].name.replace(".jsonl", "")
        out = OUT_DIR / day
        out.mkdir(parents=True, exist_ok=True)
        label_path = out / "labels.jsonl"
        with label_path.open("w", encoding="utf-8") as fh:
            for obs in observations:
                lab = _label(compact.get(obs["ticker"], []), ts_by_ticker.get(obs["ticker"], []), obs)
                fh.write(json.dumps({**obs, "labels": lab["labels"]}, sort_keys=True, default=str) + "\n")
        print(f"\nlabels written -> {label_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())