#!/usr/bin/env python3
"""Polymarket CLOB canonical extractor + integrity report (read-only).

Parses raw marketData -> PolymarketBookState and trade -> PolymarketTradeEvent,
then reports integrity diagnostics. Emits canonical records only; never computes
P1-P5 statistics.

Usage:
    .venv/Scripts/python.exe scripts/polymarket_extractor.py --day 2026-09-22 --max-lines 300000
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
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
)

MD_ROOT = ROOT / "data" / "us" / "market_data" / "raw"
TRADE_ROOT = ROOT / "data" / "us" / "trade"


def _iter(paths):
    for p in paths:
        op = gzip.open if p.suffix == ".gz" else open
        with op(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line


def _parse_line(line):
    try:
        outer = json.loads(line)
        cj = outer.get("canonical_json")
        if not isinstance(cj, str):
            return None, None, None
        return outer, json.loads(cj), cj
    except Exception:
        return None, None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-09-22")
    ap.add_argument("--max-lines", type=int, default=300_000)
    ap.add_argument("--max-trades", type=int, default=200_000)
    args = ap.parse_args()

    md_files = sorted((MD_ROOT / args.day.replace("-", "/")).glob("polymarket_us_retail_market_data-*.jsonl"))
    trade_files = sorted(TRADE_ROOT.rglob("*.jsonl"))

    stats = Counter()
    seen_sha = set()
    books_by_market = defaultdict(list)   # marketSlug -> list of (venue_ts_ms, mid, spread)
    book_markets = set()
    trade_markets = set()
    spreads = []
    depths = []

    for line in _iter(md_files):
        if stats["book_lines"] >= args.max_lines:
            break
        stats["book_lines"] += 1
        outer, inner, cj = _parse_line(line)
        if outer is None or inner is None:
            continue
        sha = outer.get("sha256") or ""
        if sha in seen_sha:
            stats["duplicate_records"] += 1
            continue
        seen_sha.add(sha)
        b = parse_book(outer, inner, cj)
        if b is None:
            stats["invalid_books"] += 1
            continue
        stats["book_snapshots_seen"] += 1
        if b.gap_clean:
            stats["hash_clean"] += 1
        else:
            stats["hash_breaks"] += 1
        book_markets.add(b.market_id)
        spreads.append(float(b.spread))
        depths.append(float(b.bid_depth_k + b.ask_depth_k))
        if b.gap_clean and b.mid is not None:
            books_by_market[b.market_id].append((b.venue_ts_ms, b.mid, b.spread))

    for line in _iter(trade_files):
        if stats["trade_lines"] >= args.max_trades:
            break
        stats["trade_lines"] += 1
        outer, inner, cj = _parse_line(line)
        if outer is None or inner is None:
            continue
        sha = outer.get("sha256") or ""
        if sha in seen_sha:
            stats["trade_duplicate"] += 1
            continue
        seen_sha.add(sha)
        t = parse_trade(outer, inner, cj)
        if t is None:
            stats["invalid_trades"] += 1
            continue
        stats["trade_records_seen"] += 1
        if t.yes_direction != 0:
            stats["classified_trades"] += 1
        trade_markets.add(t.market_id)

    # label coverage by horizon (on gap-clean book states)
    for m, arr in books_by_market.items():
        arr.sort(key=lambda x: x[0])
    books_by_market = {m: arr for m, arr in books_by_market.items() if arr}
    ts_lists = {m: [x[0] for x in arr] for m, arr in books_by_market.items()}
    mids = {m: [x[1] for x in arr] for m, arr in books_by_market.items()}
    spreads_ = {m: [x[2] for x in arr] for m, arr in books_by_market.items()}

    label_total = {f"{h}s": 0 for h in MARKOUT_HORIZONS}
    label_ok = {f"{h}s": 0 for h in MARKOUT_HORIZONS}
    for m, arr in books_by_market.items():
        ts = ts_lists[m]
        for i, (t, mid, _) in enumerate(arr):
            if mid is None:
                continue
            for h in MARKOUT_HORIZONS:
                label_total[f"{h}s"] += 1
                target = t + int(h * 1000)
                tol = int(MARKOUT_TOLERANCE[h] * 1000)
                j = bisect_left(ts, target)
                hit = False
                for k in (j - 1, j):
                    if 0 <= k < len(ts) and abs(ts[k] - target) <= tol and mids[m][k] is not None:
                        hit = True
                        break
                if hit:
                    label_ok[f"{h}s"] += 1

    # forward clusters
    def _clusters(records):
        c = set()
        for r in records:
            if r[0] >= BOUNDARY_MS:
                c.add((r[1], datetime.fromtimestamp(r[0] / 1000, UTC).date().isoformat()))
        return c

    book_clusters = set()
    for m, arr in books_by_market.items():
        for (t, mid, _) in arr:
            if t >= BOUNDARY_MS:
                book_clusters.add((m, datetime.fromtimestamp(t / 1000, UTC).date().isoformat()))
    trade_clusters = set()
    for line in _iter(trade_files):
        outer, inner, cj = _parse_line(line)
        if outer is None or inner is None:
            continue
        t = parse_trade(outer, inner, cj)
        if t is not None and t.venue_ts_ms >= BOUNDARY_MS:
            trade_clusters.add((t.market_id, datetime.fromtimestamp(t.venue_ts_ms / 1000, UTC).date().isoformat()))

    print("=== POLYMARKET CLOB EXTRACTION INTEGRITY REPORT ===")
    print(f"day: {args.day}   forward boundary: {BOUNDARY_MS}")
    print(f"book snapshots seen: {stats['book_snapshots_seen']}")
    print(f"trade records seen:  {stats['trade_records_seen']}")
    print(f"markets (book): {len(book_markets)}   markets (trade): {len(trade_markets)}")
    print(f"hash-chain breaks: {stats['hash_breaks']}   hash clean: {stats['hash_clean']}")
    print(f"duplicate records: {stats['duplicate_records']} (+{stats['trade_duplicate']} trade)")
    print(f"invalid books: {stats['invalid_books']}   invalid trades: {stats['invalid_trades']}")
    two_sided = stats['book_snapshots_seen']
    print(f"two-sided fraction: {two_sided/max(1,stats['book_snapshots_seen']):.4f}")
    if spreads:
        s = sorted(spreads)
        print(f"spread p50={s[len(s)//2]:.4f} p95={s[int(0.95*len(s))]:.4f}")
    if depths:
        d = sorted(depths)
        print(f"top-5 depth p50={d[len(d)//2]:.1f} p95={d[int(0.95*len(d))]:.1f}")
    print(f"trade-to-book match (trade markets with a book): {len(trade_markets & book_markets)}/{len(trade_markets)}")
    classified = stats.get('classified_trades', 0)
    total_trades = stats.get('trade_records_seen', 0)
    print(f"maker/taker classification coverage: {classified}/{total_trades} = {classified/max(1,total_trades):.3f}")
    print("\nlabel coverage by horizon:")
    for h in MARKOUT_HORIZONS:
        hk = f"{h}s"
        tot = label_total[hk]
        ok = label_ok[hk]
        print(f"  {hk:>8s}  {ok}/{tot}  {ok/max(1,tot):.3f}")
    print(f"\nforward clusters (book-based, P1/P2/P5): {len(book_clusters)}")
    print(f"forward clusters (trade-based, P3/P4):  {len(trade_clusters)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())