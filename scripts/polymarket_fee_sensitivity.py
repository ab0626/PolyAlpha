#!/usr/bin/env python3
"""Fee-model sensitivity check (non-hypothesis, net layer only).

Recomputes P1/P2/P5 net effect + clustered CI + KILL decision under two cost
scenarios for UNKNOWN historical fee coefficients:
  frozen:   all slugs fee = 0.0695 * p * (1-p)
  low-cost: UNKNOWN slugs fee = 0; known slugs = 0.0695

Does NOT recompute gross, hypotheses, horizons, filters, gross p, or liquidity
strata. Reads the P1-P5 evaluation manifest to use the same admitted files.

Usage:
    .venv/Scripts/python.exe scripts/polymarket_fee_sensitivity.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.polymarket.canonical import (  # noqa: E402
    BOUNDARY_MS,
    MARKOUT_TOLERANCE,
    parse_book,
)

EVAL_REPORT = ROOT / "data" / "us" / "polymarket_microstructure_eval" / "evaluation_report.json"
RAW_MARKETS = ROOT / "data" / "us" / "polymarket_fee_manifest" / "raw_markets.json"

SEED = 42
N_BOOT = 2000
HORIZONS = {"P1": 1.0, "P2": 5.0, "P5": 30.0}
THETA = Decimal("0.0695")


def _fee(p):
    return THETA * p * (Decimal(1) - p)


def _parse_line(line):
    try:
        outer = json.loads(line)
        cj = outer.get("canonical_json")
        if not isinstance(cj, str):
            return None, None, None
        return outer, json.loads(cj), cj
    except Exception:
        return None, None, None


def _lookup(ts, mids, spreads, t, horizon):
    target = t + int(horizon * 1000)
    tol = int(MARKOUT_TOLERANCE[horizon] * 1000)
    i = bisect_left(ts, target)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(ts) and abs(ts[j] - target) <= tol and mids[j] is not None:
            if best is None or abs(ts[j] - target) < abs(best[0] - target):
                best = (ts[j], mids[j], spreads[j])
    return (best[1], best[2]) if best else None


def _bootstrap(net_by_cluster):
    keys = [k for k, v in net_by_cluster.items() if v]
    rng = random.Random(SEED)
    means = []
    for _ in range(N_BOOT):
        vals = []
        for _ in range(len(keys)):
            vals.extend(net_by_cluster[keys[rng.randrange(len(keys))]])
        if vals:
            means.append(sum(vals) / len(vals))
    return means


def main() -> int:
    known = set()
    if RAW_MARKETS.exists():
        known = {m.get("slug") for m in json.loads(RAW_MARKETS.read_text(encoding="utf-8")) if m.get("slug")}
    ev = json.loads(EVAL_REPORT.read_text(encoding="utf-8"))
    files = [ROOT / f for f in ev.get("manifest", {}).keys()]
    print(f"known API slugs: {len(known)}   evaluation files: {len(files)}")

    books = defaultdict(list)
    slug_seen = set()
    for f in files:
        op = gzip.open if f.suffix == ".gz" else open
        with op(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                outer, inner, cj = _parse_line(line)
                if inner is None:
                    continue
                b = parse_book(outer, inner, cj)
                if b is not None and b.integrity_verified and b.exchange_timestamp_ms >= BOUNDARY_MS:
                    books[b.market_slug].append((b.exchange_timestamp_ms, b.mid, b.spread, b.imbalance,
                                                 b.microprice, b.top5_bid_depth, b.top5_ask_depth))
                    slug_seen.add(b.market_slug)
    unknown = slug_seen - known
    print(f"evaluation slugs: {len(slug_seen)}   UNKNOWN (not in API): {len(unknown)}")

    T = {}
    for m, arr in books.items():
        arr.sort(key=lambda x: x[0])
        T[m] = {"ts": [x[0] for x in arr], "mid": [x[1] for x in arr], "spread": [x[2] for x in arr],
                "imbalance": [x[3] for x in arr], "microprice": [x[4] for x in arr],
                "bid_depth": [x[5] for x in arr], "ask_depth": [x[6] for x in arr]}

    # obs per hypothesis: (cluster_key, gross, fee_contrib, half_spread_contrib, is_unknown)
    obs = {h: [] for h in HORIZONS}
    for m, s in T.items():
        ts, mids, spreads, imb, micro, bd, ad = s["ts"], s["mid"], s["spread"], s["imbalance"], s["microprice"], s["bid_depth"], s["ask_depth"]
        unk = m in unknown
        for i, (t, mid) in enumerate(zip(ts, mids)):
            if mid is None:
                continue
            day = datetime.fromtimestamp(t / 1000, UTC).date().isoformat()
            ck = (m, day)
            if imb[i] is not None and imb[i] != 0:
                fut = _lookup(ts, mids, spreads, t, 1.0)
                if fut:
                    sgn = Decimal(1) if imb[i] > 0 else Decimal(-1)
                    raw = sgn * (fut[0] - mid)
                    obs["P1"].append((ck, float(raw), float(_fee(fut[0])), float(Decimal("0.5") * (fut[1] or Decimal(0))), unk))
            if micro[i] is not None and (micro[i] - mid) != 0:
                fut = _lookup(ts, mids, spreads, t, 5.0)
                if fut:
                    sgn = Decimal(1) if (micro[i] - mid) > 0 else Decimal(-1)
                    raw = sgn * (fut[0] - mid)
                    obs["P2"].append((ck, float(raw), float(_fee(fut[0])), float(Decimal("0.5") * (fut[1] or Decimal(0))), unk))
            j = bisect_left(ts, t - 5000)
            if j < i:
                dlt = (ad[i] - ad[j]) - (bd[i] - bd[j])
                if dlt != 0:
                    fut = _lookup(ts, mids, spreads, t, 30.0)
                    if fut:
                        sgn = Decimal(1) if dlt > 0 else Decimal(-1)
                        raw = sgn * (fut[0] - mid)
                        obs["P5"].append((ck, float(raw), float(_fee(fut[0])), float(Decimal("0.5") * (fut[1] or Decimal(0))), unk))

    for h in ("P1", "P2", "P5"):
        frozen_net = defaultdict(list)
        lowcost_net = defaultdict(list)
        for (ck, gross, fee, halfspread, unk) in obs[h]:
            frozen_net[ck].append(gross - halfspread - fee)
            lowcost_net[ck].append(gross - halfspread - (0.0 if unk else fee))
        fb = sorted(_bootstrap(frozen_net))
        lb = sorted(_bootstrap(lowcost_net))
        f_lo, f_hi = fb[int(0.025 * len(fb))], fb[int(0.975 * len(fb))]
        l_lo, l_hi = lb[int(0.025 * len(lb))], lb[int(0.975 * len(lb))]
        n_clusters = len([k for k, v in frozen_net.items() if v])
        f_net = sum(sum(v) for v in frozen_net.values()) / sum(len(v) for v in frozen_net.values())
        l_net = sum(sum(v) for v in lowcost_net.values()) / sum(len(v) for v in lowcost_net.values())
        f_kill = (n_clusters >= 40) and (f_hi < 0)
        l_kill = (n_clusters >= 40) and (l_hi < 0)
        print(f"\n{h}: clusters={n_clusters} obs={len(obs[h])} unknown_obs={sum(1 for o in obs[h] if o[4])}")
        print(f"  FROZEN   net={f_net:.6f}  CI=[{f_lo:.6f},{f_hi:.6f}]  KILL={f_kill}")
        print(f"  LOW-COST net={l_net:.6f}  CI=[{l_lo:.6f},{l_hi:.6f}]  KILL={l_kill}")
        print(f"  VERDICT: {'provenance-only (KILL survives)' if (f_kill and l_kill) else ('fee-dependent (KILL does NOT survive Θ=0)' if (f_kill and not l_kill) else 'n/a')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())