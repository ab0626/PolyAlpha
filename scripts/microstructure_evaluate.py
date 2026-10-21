#!/usr/bin/env python3
"""MICROSTRUCTURE-EVIDENCE-V1 — H1..H5 forward evaluation (frozen contract).

Runs the pre-registered primary-horizon tests EXACTLY ONCE against the forward
partition, with day-clustered bootstrap (seed 42) and Holm-Bonferroni across the
five primary endpoints. Reads existing captures only; writes an immutable report.

Usage:
    .venv/Scripts/python.exe scripts/microstructure_evaluate.py [--days 1]
"""

from __future__ import annotations

import argparse
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

from polyalpha.energy.book_reconstruction import (  # noqa: E402
    EVIDENCE_PARTITION_BOUNDARY,
    MARKOUT_TOLERANCE,
    Reconstructor,
)

WS_DIR = ROOT / "data" / "energy" / "kalshi" / "ws"
OUT = ROOT / "data" / "energy" / "kalshi" / "microstructure_eval"

BOUNDARY_MS = int(datetime.fromisoformat(EVIDENCE_PARTITION_BOUNDARY).timestamp() * 1000)
TAKER_FEE = Decimal("0.07")
HALF_SPREAD = Decimal("0.5")
SEED = 42
N_BOOT = 2000
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")

HORIZONS = {"H1": "1s", "H2": "5s", "H3": "5s", "H4": "1s", "H5": "30s"}
PREDICT_NEGATIVE = {"H4"}  # H4's maker_markout is predicted NEGATIVE (adverse selection)


def _dec(v):
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _trade_yes_sign(taker_side: str, taker_book_side: str) -> int:
    if (taker_side == "yes" and taker_book_side == "ask") or (taker_side == "no" and taker_book_side == "bid"):
        return 1
    return -1


def _lookup_future(ts, mids, spreads, t: int, horizon: float):
    target = t + int(horizon * 1000)
    tol = int(MARKOUT_TOLERANCE[horizon] * 1000)
    i = bisect_left(ts, target)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(ts) and abs(ts[j] - target) <= tol and mids[j] is not None:
            if best is None or abs(ts[j] - target) < abs(best[0] - target):
                best = (ts[j], mids[j], spreads[j])
    return (best[1], best[2]) if best else None


def _bootstrap(net_by_cluster: dict, n_boot: int = N_BOOT, seed: int = SEED) -> list[float]:
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


def _holm(pvals: list[float]) -> list[float]:
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [1.0] * m
    for rank, i in enumerate(order):
        adj[i] = min(1.0, pvals[i] * (m - rank))
    for r in range(1, m):
        adj[order[r]] = max(adj[order[r]], adj[order[r - 1]])
    return adj


def _control_status(obs_list, net_all) -> dict:
    """Directional-consistency PASS/FAIL for 5 controls. obs tuple =
    (ck, gross, net, spread, depth, price_mid, tte, vol)."""
    overall = sum(net_all) / len(net_all) if net_all else 0.0
    sign = 1 if overall > 0 else -1

    def _bucket(fn):
        b = defaultdict(list)
        for o, n in zip(obs_list, net_all):
            b[fn(o)].append(n)
        return b

    def _consistent(buckets, min_ok):
        tot = 0
        ok = 0
        for k, vals in buckets.items():
            if vals:
                tot += 1
                if (sum(vals) / len(vals)) * sign > 0:
                    ok += 1
        return "PASS" if (tot and ok >= min_ok) else "FAIL"

    sp = sorted(o[2] for o in obs_list); med_sp = sp[len(sp) // 2]
    dp = sorted(o[3] for o in obs_list); med_dp = dp[len(dp) // 2]
    tt = sorted(o[5] for o in obs_list); med_tt = tt[len(tt) // 2]
    vl = sorted(o[6] for o in obs_list); med_vl = vl[len(vl) // 2]
    return {
        "spread": _consistent(_bucket(lambda o: 0 if o[2] < med_sp else 1), 2),
        "depth": _consistent(_bucket(lambda o: 0 if o[3] < med_dp else 1), 2),
        "price": _consistent(_bucket(lambda o: min(4, int(o[4] // 0.2))), 4),
        "time-to-expiry": _consistent(_bucket(lambda o: 0 if o[5] < med_tt else 1), 2),
        "volatility": _consistent(_bucket(lambda o: 0 if o[6] < med_vl else 1), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--files", nargs="*", default=None)
    args = ap.parse_args()

    files = [Path(f) for f in args.files] if args.files else sorted(p for p in WS_DIR.glob("*.jsonl") if _DAY_RE.match(p.name))[-args.days:]

    rec = Reconstructor()
    books: dict[str, list] = defaultdict(list)
    trades: dict[str, list] = defaultdict(list)

    for f in files:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                if '"orderbook_' not in line and '"trade"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t in ("orderbook_snapshot", "orderbook_delta"):
                    bs = rec.apply(d)
                    if bs is None or not bs.gap_clean or bs.venue_ts_ms < BOUNDARY_MS or bs.mid is None:
                        continue
                    books[bs.ticker].append((bs.venue_ts_ms, bs.mid, bs.spread, bs.imbalance,
                                             bs.microprice, bs.bid_depth_k, bs.ask_depth_k))
                elif t == "trade":
                    m = (d.get("message") or {}).get("msg") or {}
                    ts = m.get("ts_ms")
                    tk = m.get("market_ticker")
                    if not isinstance(ts, (int, float)) or not tk or ts < BOUNDARY_MS:
                        continue
                    count = _dec(m.get("count_fp"))
                    if count is None:
                        continue
                    sign = _trade_yes_sign(m.get("taker_side") or "", m.get("taker_book_side") or "")
                    trades[tk].append((int(ts), sign, count))

    T = {}
    for tk in books:
        books[tk].sort(key=lambda x: x[0])
        arr = books[tk]
        T[tk] = {
            "ts": [x[0] for x in arr], "mid": [x[1] for x in arr], "spread": [x[2] for x in arr],
            "imbalance": [x[3] for x in arr], "microprice": [x[4] for x in arr],
            "bid_depth": [x[5] for x in arr], "ask_depth": [x[6] for x in arr],
        }
    Tt = {}
    for tk in trades:
        trades[tk].sort(key=lambda x: x[0])
        Tt[tk] = {"ts": [x[0] for x in trades[tk]], "sign": [x[1] for x in trades[tk]], "count": [x[2] for x in trades[tk]]}

    obs = {h: [] for h in ("H1", "H2", "H3", "H4", "H5")}

    for tk, s in T.items():
        ts, mids, spreads, imb, micro, bd, ad = s["ts"], s["mid"], s["spread"], s["imbalance"], s["microprice"], s["bid_depth"], s["ask_depth"]
        tmin, tmax = ts[0], ts[-1]
        for i, (t, mid) in enumerate(zip(ts, mids)):
            if mid is None:
                continue
            day = datetime.fromtimestamp(t / 1000, UTC).date().isoformat()
            ck = (tk, day)
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
                fut = _lookup_future(ts, mids, spreads, t, 1.0)
                if fut:
                    sgn = Decimal(1) if imb[i] > 0 else Decimal(-1)
                    raw = sgn * (fut[0] - mid)
                    net = raw - HALF_SPREAD * (fut[1] or Decimal(0)) - TAKER_FEE * fut[0]
                    obs["H1"].append((ck, float(raw), float(net), float(spread), float(depth), float(mid), tte, float(vol)))

            if micro[i] is not None:
                disp = micro[i] - mid
                if disp != 0:
                    fut = _lookup_future(ts, mids, spreads, t, 5.0)
                    if fut:
                        sgn = Decimal(1) if disp > 0 else Decimal(-1)
                        raw = sgn * (fut[0] - mid)
                        net = raw - HALF_SPREAD * (fut[1] or Decimal(0)) - TAKER_FEE * fut[0]
                        obs["H2"].append((ck, float(raw), float(net), float(spread), float(depth), float(mid), tte, float(vol)))

            j = bisect_left(ts, t - 5000)
            if j < i:
                dlt = (ad[i] - ad[j]) - (bd[i] - bd[j])
                if dlt != 0:
                    fut = _lookup_future(ts, mids, spreads, t, 30.0)
                    if fut:
                        sgn = Decimal(1) if dlt > 0 else Decimal(-1)
                        raw = sgn * (fut[0] - mid)
                        net = raw - HALF_SPREAD * (fut[1] or Decimal(0)) - TAKER_FEE * fut[0]
                        obs["H5"].append((ck, float(raw), float(net), float(spread), float(depth), float(mid), tte, float(vol)))

    for tk, s in Tt.items():
        tts, signs, counts = s["ts"], s["sign"], s["count"]
        b = T.get(tk)
        for i, (t, sgn, cnt) in enumerate(zip(tts, signs, counts)):
            day = datetime.fromtimestamp(t / 1000, UTC).date().isoformat()
            ck = (tk, day)
            if b is None:
                continue
            # H3: trailing 1s aggregate flow -> 5s continuation
            lo = bisect_left(tts, t - 1000)
            flow = sum(signs[k] * counts[k] for k in range(lo, i + 1))
            if flow != 0:
                fut = _lookup_future(b["ts"], b["mid"], b["spread"], t, 5.0)
                bi = bisect_right(b["ts"], t) - 1
                if fut and bi >= 0 and b["mid"][bi] is not None:
                    mid0 = b["mid"][bi]
                    sgnf = Decimal(1) if flow > 0 else Decimal(-1)
                    raw = sgnf * (fut[0] - mid0)
                    net = raw - HALF_SPREAD * (fut[1] or Decimal(0)) - TAKER_FEE * fut[0]
                    obs["H3"].append((ck, float(raw), float(net), 0.0, 0.0, float(mid0), 0.5, 0.0))
            # H4: maker-taker adverse selection -> 1s (maker_markout = -s * dmid)
            fut = _lookup_future(b["ts"], b["mid"], b["spread"], t, 1.0)
            bi = bisect_right(b["ts"], t) - 1
            if fut and bi >= 0 and b["mid"][bi] is not None:
                mid0 = b["mid"][bi]
                maker = Decimal(-1) * Decimal(sgn) * (fut[0] - mid0)
                maker_net = maker - HALF_SPREAD * (fut[1] or Decimal(0)) - TAKER_FEE * fut[0]
                obs["H4"].append((ck, float(maker), float(maker_net), 0.0, 0.0, float(mid0), 0.5, 0.0))

    results = {}
    order = ["H1", "H2", "H3", "H4", "H5"]
    for h in order:
        o = obs[h]
        if not o:
            results[h] = {"n_raw": 0, "n_labeled": 0, "clusters": 0, "decision": "CONTINUE"}
            continue
        by_cluster = defaultdict(list)
        gross_all, net_all = [], []
        for (ck, g, n, *_) in o:
            by_cluster[ck].append(n)
            gross_all.append(g)
            net_all.append(n)
        clusters = list(by_cluster.keys())
        gross = sum(gross_all) / len(gross_all)
        net = sum(net_all) / len(net_all)
        boot = _bootstrap(by_cluster)
        boot.sort()
        lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot))]
        p_raw = (sum(1 for x in boot if x >= 0) if h in PREDICT_NEGATIVE else sum(1 for x in boot if x <= 0)) / len(boot)
        results[h] = {
            "primary_horizon": HORIZONS[h],
            "n_raw": len(o),
            "n_labeled": len(o),
            "tickers": len(set(ck[0] for ck in clusters)),
            "operating_days": len(set(ck[1] for ck in clusters)),
            "clusters": len(clusters),
            "gross": gross,
            "net": net,
            "ci": (lo, hi),
            "p_raw": p_raw,
            "control_status": _control_status(o, net_all) if h not in ("H3", "H4") else {},
        }

    holm = _holm([results[h].get("p_raw", 1.0) for h in order])
    for i, h in enumerate(order):
        r = results[h]
        n = r.get("clusters", 0)
        net = r.get("net", 0.0)
        lo, hi = r.get("ci", (0.0, 0.0))
        p_holm = holm[i]
        neg = h in PREDICT_NEGATIVE
        cs = r.get("control_status", {})
        promoted = (n >= 160 and p_holm < 0.05 and ((not neg and net > 0) or (neg and net < 0)))
        if cs:
            promoted = promoted and all(v == "PASS" for v in cs.values())
        # Futility (KILL) is only evaluated at a milestone (N >= 40), per the
        # frozen contract's "at any milestone" language.
        killed = (n >= 40) and ((not neg and hi < 0) or (neg and lo > 0))
        r["p_holm"] = p_holm
        r["decision"] = "PROMOTE" if promoted else ("KILL" if killed else "CONTINUE")

    OUT.mkdir(parents=True, exist_ok=True)
    rep_path = OUT / "evaluation_report.json"
    rep_path.write_text(json.dumps({
        "evaluated_at": datetime.now(UTC).isoformat(),
        "boundary": EVIDENCE_PARTITION_BOUNDARY,
        "files": [f.name for f in files],
        "seed": SEED, "n_boot": N_BOOT,
        "results": results,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("=== MICROSTRUCTURE-EVIDENCE-V1 — H1..H5 forward evaluation ===")
    print(f"files: {[f.name for f in files]}   boundary: {EVIDENCE_PARTITION_BOUNDARY}")
    for h in order:
        r = results[h]
        print(f"\n{h} — primary horizon {r.get('primary_horizon', '?')}")
        print(f"  raw observations:      {r.get('n_raw')}")
        print(f"  labeled observations:  {r.get('n_labeled')}")
        print(f"  tickers:               {r.get('tickers')}")
        print(f"  operating days:        {r.get('operating_days')}")
        print(f"  independent clusters:  {r.get('clusters')}")
        print(f"  gross effect:          {round(r.get('gross', 0.0), 6)}")
        print(f"  net effect:            {round(r.get('net', 0.0), 6)}")
        print(f"  95% clustered CI:      [{round(r['ci'][0], 6)}, {round(r['ci'][1], 6)}]")
        print(f"  raw p:                 {round(r.get('p_raw', 1.0), 4)}")
        print(f"  Holm-adjusted p:       {round(r.get('p_holm', 1.0), 4)}")
        for k, v in r.get("control_status", {}).items():
            print(f"    control {k:16s} {v}")
        print(f"  DECISION:              {r['decision']}")
    print(f"\nreport written -> {rep_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())