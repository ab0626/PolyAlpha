#!/usr/bin/env python3
"""POLYMARKET-MICROSTRUCTURE-V1 — P1..P5 forward evaluation (frozen contract).

Runs the pre-registered primary-horizon tests EXACTLY ONCE against the forward
partition, with day-clustered bootstrap (seed 42), Holm-Bonferroni, and
pre-registered top-5-depth liquidity strata. Records an evidence manifest (file
SHA-256 + byte sizes) so the archive cannot drift underneath the result.

Usage:
    .venv/Scripts/python.exe scripts/polymarket_evaluate.py --days 2
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import random
import re
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.polymarket.canonical import (  # noqa: E402
    BOUNDARY_MS,
    MARKOUT_HORIZONS,
    MARKOUT_TOLERANCE,
    parse_book,
    parse_trade,
    taker_fee,
)

MD_ROOT = ROOT / "data" / "us" / "market_data" / "raw"
TRADE_ROOT = ROOT / "data" / "us" / "trade"
OUT = ROOT / "data" / "us" / "polymarket_microstructure_eval"
SEED = 42
N_BOOT = 2000
HORIZONS = {"P1": "1s", "P2": "5s", "P3": "5s", "P4": "1s", "P5": "30s"}
PREDICT_NEGATIVE = {"P4"}


def _dec(v):
    try:
        return Decimal(str(v))
    except Exception:
        return None


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


def _bootstrap(net_by_cluster, seed=SEED, n_boot=N_BOOT):
    keys = [k for k, v in net_by_cluster.items() if v]
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        vals = []
        for _ in range(len(keys)):
            vals.extend(net_by_cluster[keys[rng.randrange(len(keys))]])
        if vals:
            means.append(sum(vals) / len(vals))
    return means


def _holm(pvals):
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [1.0] * m
    for rank, i in enumerate(order):
        adj[i] = min(1.0, pvals[i] * (m - rank))
    for r in range(1, m):
        adj[order[r]] = max(adj[order[r]], adj[order[r - 1]])
    return adj


def _liquidity_strata(net_all, obs_meta):
    """obs_meta[i] = top-5 depth. Bucket into 4 quartiles; PASS if sign consistent in >=2."""
    depths = sorted(d for d in obs_meta)
    qs = [depths[int(p * len(depths))] for p in (0.25, 0.5, 0.75)]
    buckets = defaultdict(list)
    for d, n in zip(obs_meta, net_all):
        b = 0
        for qi, q in enumerate(qs):
            if d >= q:
                b = qi + 1
        buckets[b].append(n)
    sign = 1 if (sum(net_all) / len(net_all)) > 0 else -1
    ok = 0
    tot = 0
    for b, vals in sorted(buckets.items()):
        if vals:
            tot += 1
            if (sum(vals) / len(vals)) * sign > 0:
                ok += 1
    return "PASS" if (tot and ok >= 2) else "FAIL", {b: round(sum(v) / len(v), 6) for b, v in sorted(buckets.items()) if v}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2, help="forward market-data days to process")
    ap.add_argument("--max-trades", type=int, default=300_000)
    args = ap.parse_args()

    day_dirs = sorted({p.parent for p in MD_ROOT.rglob("polymarket_us_retail_market_data-*.jsonl")})
    files = []
    for d in day_dirs[-args.days:]:
        files.extend(sorted(d.glob("polymarket_us_retail_market_data-*.jsonl")))
    trade_files = sorted(TRADE_ROOT.rglob("*.jsonl"))

    manifest = {}
    for f in files:
        h = hashlib.sha256()
        size = 0
        with (gzip.open(f, "rb") if f.suffix == ".gz" else open(f, "rb")) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
                size += len(chunk)
        manifest[str(f.relative_to(ROOT))] = {"bytes": size, "sha256": h.hexdigest()}

    books = defaultdict(list)
    trades = defaultdict(list)
    book_parsed = trade_parsed = 0
    for f in files:
        op = gzip.open if f.suffix == ".gz" else open
        with op(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                outer, inner, cj = _parse_line(line)
                if inner is None:
                    continue
                b = parse_book(outer, inner, cj)
                if b is not None and b.integrity_verified and b.exchange_timestamp_ms >= BOUNDARY_MS:
                    books[b.market_slug].append((b.exchange_timestamp_ms, b.mid, b.spread, b.imbalance,
                                                 b.microprice, b.top5_bid_depth, b.top5_ask_depth))
                    book_parsed += 1
    for f in trade_files:
        op = gzip.open if f.suffix == ".gz" else open
        with op(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                outer, inner, cj = _parse_line(line)
                if inner is None:
                    continue
                t = parse_trade(outer, inner, cj)
                if t is not None and t.integrity_verified and t.classification_status in ("COMPLETE", "MISSING_INTENT") \
                        and t.yes_direction != 0 and t.trade_time_ms >= BOUNDARY_MS:
                    trades[t.market_slug].append((t.trade_time_ms, t.yes_direction, t.size))
                    trade_parsed += 1

    T = {}
    for m, arr in books.items():
        arr.sort(key=lambda x: x[0])
        T[m] = {"ts": [x[0] for x in arr], "mid": [x[1] for x in arr], "spread": [x[2] for x in arr],
                "imbalance": [x[3] for x in arr], "microprice": [x[4] for x in arr],
                "bid_depth": [x[5] for x in arr], "ask_depth": [x[6] for x in arr]}
    Tt = {}
    for m, arr in trades.items():
        arr.sort(key=lambda x: x[0])
        Tt[m] = {"ts": [x[0] for x in arr], "sign": [x[1] for x in arr], "size": [x[2] for x in arr]}

    obs = {h: [] for h in ("P1", "P2", "P3", "P4", "P5")}

    for m, s in T.items():
        ts, mids, spreads, imb, micro, bd, ad = s["ts"], s["mid"], s["spread"], s["imbalance"], s["microprice"], s["bid_depth"], s["ask_depth"]
        tmin, tmax = ts[0], ts[-1]
        for i, (t, mid) in enumerate(zip(ts, mids)):
            if mid is None:
                continue
            day = datetime.fromtimestamp(t / 1000, UTC).date().isoformat()
            ck = (m, day)
            tte = (t - tmin) / (tmax - tmin) if tmax > tmin else 0.5
            depth = bd[i] + ad[i]
            spread = spreads[i] if spreads[i] is not None else Decimal(0)
            vol = Decimal(0)
            if i >= 2:
                rs = [float((mids[k] / mids[k - 1]).ln()) for k in range(max(0, i - 30), i)
                      if mids[k] and mids[k - 1] and mids[k] > 0 and mids[k - 1] > 0]
                if len(rs) >= 2:
                    mu = sum(rs) / len(rs)
                    vol = Decimal(str(math.sqrt(sum((r - mu) ** 2 for r in rs) / (len(rs) - 1))))
            if imb[i] is not None and imb[i] != 0:
                fut = _lookup(ts, mids, spreads, t, 1.0)
                if fut:
                    sgn = Decimal(1) if imb[i] > 0 else Decimal(-1)
                    raw = sgn * (fut[0] - mid)
                    net = raw - Decimal("0.5") * (fut[1] or Decimal(0)) - taker_fee(fut[0])
                    obs["P1"].append((ck, float(raw), float(net), float(spread), float(depth), float(mid), tte, float(vol)))
            if micro[i] is not None:
                disp = micro[i] - mid
                if disp != 0:
                    fut = _lookup(ts, mids, spreads, t, 5.0)
                    if fut:
                        sgn = Decimal(1) if disp > 0 else Decimal(-1)
                        raw = sgn * (fut[0] - mid)
                        net = raw - Decimal("0.5") * (fut[1] or Decimal(0)) - taker_fee(fut[0])
                        obs["P2"].append((ck, float(raw), float(net), float(spread), float(depth), float(mid), tte, float(vol)))
            j = bisect_left(ts, t - 5000)
            if j < i:
                dlt = (ad[i] - ad[j]) - (bd[i] - bd[j])
                if dlt != 0:
                    fut = _lookup(ts, mids, spreads, t, 30.0)
                    if fut:
                        sgn = Decimal(1) if dlt > 0 else Decimal(-1)
                        raw = sgn * (fut[0] - mid)
                        net = raw - Decimal("0.5") * (fut[1] or Decimal(0)) - taker_fee(fut[0])
                        obs["P5"].append((ck, float(raw), float(net), float(spread), float(depth), float(mid), tte, float(vol)))

    for m, s in Tt.items():
        tts, signs, sizes = s["ts"], s["sign"], s["size"]
        b = T.get(m)
        for i, (t, sgn, sz) in enumerate(zip(tts, signs, sizes)):
            day = datetime.fromtimestamp(t / 1000, UTC).date().isoformat()
            ck = (m, day)
            if b is None:
                continue
            lo = bisect_left(tts, t - 1000)
            flow = sum(signs[k] * sizes[k] for k in range(lo, i + 1))
            if flow != 0:
                fut = _lookup(b["ts"], b["mid"], b["spread"], t, 5.0)
                bi = bisect_right(b["ts"], t) - 1
                if fut and bi >= 0 and b["mid"][bi] is not None:
                    mid0 = b["mid"][bi]
                    sgnf = Decimal(1) if flow > 0 else Decimal(-1)
                    raw = sgnf * (fut[0] - mid0)
                    net = raw - Decimal("0.5") * (fut[1] or Decimal(0)) - taker_fee(fut[0])
                    obs["P3"].append((ck, float(raw), float(net), 0.0, 0.0, float(mid0), 0.5, 0.0))
            fut = _lookup(b["ts"], b["mid"], b["spread"], t, 1.0)
            bi = bisect_right(b["ts"], t) - 1
            if fut and bi >= 0 and b["mid"][bi] is not None:
                mid0 = b["mid"][bi]
                maker = Decimal(-1) * Decimal(sgn) * (fut[0] - mid0)
                maker_net = maker - Decimal("0.5") * (fut[1] or Decimal(0)) - taker_fee(fut[0])
                obs["P4"].append((ck, float(maker), float(maker_net), 0.0, 0.0, float(mid0), 0.5, 0.0))

    order = ["P1", "P2", "P3", "P4", "P5"]
    results = {}
    for h in order:
        o = obs[h]
        if not o:
            results[h] = {"n_raw": 0, "clusters": 0, "decision": "CONTINUE"}
            continue
        by_cluster = defaultdict(list)
        gross_all, net_all, meta = [], [], []
        for (ck, g, n, *rest) in o:
            by_cluster[ck].append(n)
            gross_all.append(g)
            net_all.append(n)
            meta.append(rest[1])  # depth
        clusters = list(by_cluster.keys())
        gross = sum(gross_all) / len(gross_all)
        net = sum(net_all) / len(net_all)
        boot = _bootstrap(by_cluster)
        boot.sort()
        lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot))]
        p_raw = (sum(1 for x in boot if x >= 0) if h in PREDICT_NEGATIVE else sum(1 for x in boot if x <= 0)) / len(boot)
        liq_status, liq_means = _liquidity_strata(net_all, meta) if h not in ("P3", "P4") else ("N/A", {})
        results[h] = {
            "primary_horizon": HORIZONS[h], "n_raw": len(o), "n_labeled": len(o),
            "tickers": len(set(ck[0] for ck in clusters)), "operating_days": len(set(ck[1] for ck in clusters)),
            "clusters": len(clusters), "gross": gross, "net": net, "ci": (lo, hi),
            "p_raw": p_raw, "liquidity_strata": liq_status, "liquidity_means": liq_means,
        }

    holm = _holm([results[h].get("p_raw", 1.0) for h in order])
    for i, h in enumerate(order):
        r = results[h]
        n = r.get("clusters", 0)
        net = r.get("net", 0.0)
        lo, hi = r.get("ci", (0.0, 0.0))
        p_holm = holm[i]
        neg = h in PREDICT_NEGATIVE
        promoted = (n >= 160 and p_holm < 0.05 and ((not neg and net > 0) or (neg and net < 0)))
        killed = (n >= 40) and ((not neg and hi < 0) or (neg and lo > 0))
        r["p_holm"] = p_holm
        r["decision"] = "PROMOTE" if promoted else ("KILL" if killed else "CONTINUE")

    OUT.mkdir(parents=True, exist_ok=True)
    rep_path = OUT / "evaluation_report.json"
    rep_path.write_text(json.dumps({
        "evaluated_at": datetime.now(UTC).isoformat(), "boundary": BOUNDARY_MS,
        "book_records": book_parsed, "trade_records": trade_parsed,
        "manifest": manifest, "seed": SEED, "n_boot": N_BOOT,
        "fee_model": "taker=0.0695*p*(1-p), maker_rebate=0 (Tier 1)",
        "results": results,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("=== POLYMARKET-MICROSTRUCTURE-V1 — P1..P5 forward evaluation ===")
    print(f"book records: {book_parsed}   trade records: {trade_parsed}   files: {len(manifest)}")
    for h in order:
        r = results[h]
        print(f"\n{h} — {r.get('primary_horizon','?')}  clusters={r.get('clusters')}  obs={r.get('n_raw')}")
        print(f"  gross={round(r.get('gross',0),6)}  net={round(r.get('net',0),6)}  CI=[{round(r['ci'][0],6)},{round(r['ci'][1],6)}]")
        print(f"  raw p={round(r.get('p_raw',1),4)}  Holm p={round(r.get('p_holm',1),4)}  liquidity={r.get('liquidity_strata')}")
        print(f"  liquidity strata means: {r.get('liquidity_means')}")
        print(f"  DECISION: {r['decision']}")
    print(f"\nreport -> {rep_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())