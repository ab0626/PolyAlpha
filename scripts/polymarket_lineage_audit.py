#!/usr/bin/env python3
"""Polymarket CLOB lineage + semantics audit (read-only).

Answers the pre-contract questions for POLYMARKET_MICROSTRUCTURE_EVIDENCE_V1:
snapshot semantics, ordering, gap provability, timestamps, tick size, fee info,
outcome-token mapping, maker/taker fields, two-sidedness, and clustering.

Usage:
    .venv/Scripts/python.exe scripts/polymarket_lineage_audit.py [--day 2026-09-21]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MD_ROOT = ROOT / "data" / "us" / "market_data" / "raw"
TRADE_ROOT = ROOT / "data" / "us" / "trade"


def _iter_lines(paths):
    for p in paths:
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line, p


def _parse(line):
    try:
        outer = json.loads(line)
        cj = outer.get("canonical_json")
        if not isinstance(cj, str):
            return outer, None, None
        inner = json.loads(cj)
        return outer, inner, cj
    except Exception:
        return None, None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-09-21")
    ap.add_argument("--max-lines", type=int, default=400_000)
    args = ap.parse_args()

    md_files = sorted((MD_ROOT / args.day.replace("-", "/")).glob("polymarket_us_retail_market_data-*.jsonl")) \
        if (MD_ROOT / args.day.replace("-", "/")).exists() else []
    trade_files = sorted(TRADE_ROOT.rglob("*.jsonl"))

    print(f"=== POLYMARKET CLOB LINEAGE AUDIT (day {args.day}) ===")
    print(f"market_data files: {len(md_files)}   trade files: {len(trade_files)}")

    # ---- market data audit ----
    hash_ok = hash_mismatch = 0
    seq_by_conn = defaultdict(list)
    ticks = Counter()
    two_sided = 0
    total_books = 0
    ts_venue_present = ts_local_present = 0
    states = Counter()
    slugs = set()
    n = 0
    px_values = set()

    for line, p in _iter_lines(md_files):
        if n >= args.max_lines:
            break
        n += 1
        outer, inner, cj = _parse(line)
        if outer is None or inner is None:
            continue
        # hash integrity: sha256 == sha256(canonical_json)
        if "sha256" in outer and cj is not None:
            if hashlib.sha256(cj.encode("utf-8")).hexdigest() == outer["sha256"]:
                hash_ok += 1
            else:
                hash_mismatch += 1
        conn = inner.get("connection_id")
        seq = inner.get("message_sequence_local")
        if conn and isinstance(seq, int):
            seq_by_conn[conn].append(seq)
        if inner.get("exchange_timestamp_ms") is not None:
            ts_venue_present += 1
        if inner.get("received_at_ns") is not None:
            ts_local_present += 1
        md = (inner.get("payload") or {}).get("marketData") or {}
        if not md:
            continue
        total_books += 1
        states[md.get("state")] += 1
        slugs.add(md.get("marketSlug"))
        bids = md.get("bids") or []
        offers = md.get("offers") or []
        if bids and offers:
            two_sided += 1
        for side in (bids, offers):
            for lvl in side:
                px = (lvl.get("px") or {}).get("value")
                if px is not None:
                    px_values.add(px)

    # tick size: min positive difference between distinct sorted prices
    def _tick(vals):
        d = sorted(Decimal(v) for v in vals)
        diffs = [d[i] - d[i - 1] for i in range(1, len(d))]
        return min((x for x in diffs if x > 0), default=None)

    # sequence continuity per connection
    seq_gaps = 0
    for conn, seqs in seq_by_conn.items():
        s = sorted(set(seqs))
        for a, b in zip(s, s[1:]):
            if b != a + 1:
                seq_gaps += 1

    print(f"\n--- market_data ---")
    print(f"sampled lines: {n}")
    print(f"hash: ok={hash_ok} mismatch={hash_mismatch}  (sha256 == sha256(canonical_json))")
    print(f"message_sequence_local gaps (unique seq, per connection): {seq_gaps}  connections: {len(seq_by_conn)}")
    print(f"venue exchange_timestamp_ms present: {ts_venue_present}/{n}   local received_at_ns present: {ts_local_present}/{n}")
    print(f"book states: {Counter(states)}")
    print(f"unique marketSlugs: {len(slugs)}")
    print(f"two-sided books (bids+offers non-empty): {two_sided}/{total_books} = {two_sided/total_books:.3f}")
    print(f"distinct price levels seen: {len(px_values)}")
    print(f"min tick (min positive price diff): {_tick(px_values)}")

    # ---- trade audit ----
    print(f"\n--- trade lineage ---")
    trade_n = 0
    maker_fields = Counter()
    taker_fields = Counter()
    has_outcome = 0
    has_intent = 0
    trade_slugs = set()
    for line, p in _iter_lines(trade_files):
        if trade_n >= 100_000:
            break
        outer, inner, cj = _parse(line)
        if outer is None or inner is None:
            continue
        tr = (inner.get("payload") or {}).get("trade") or {}
        if not tr:
            continue
        trade_n += 1
        trade_slugs.add(tr.get("marketSlug"))
        maker = tr.get("maker") or {}
        taker = tr.get("taker") or {}
        maker_fields[tuple(sorted(k for k, v in maker.items() if v not in (None, "", "OUTCOME_SIDE_UNSPECIFIED", "ORDER_ACTION_UNSPECIFIED", "ORDER_INTENT_UNDEFINED")))] += 1
        taker_fields[tuple(sorted(k for k, v in taker.items() if v not in (None, "", "OUTCOME_SIDE_UNSPECIFIED", "ORDER_ACTION_UNSPECIFIED", "ORDER_INTENT_UNDEFINED")))] += 1
        if taker.get("outcomeSide") not in (None, "OUTCOME_SIDE_UNSPECIFIED"):
            has_outcome += 1
        if taker.get("intent") not in (None, "ORDER_INTENT_UNDEFINED"):
            has_intent += 1

    print(f"trade records sampled: {trade_n}   unique marketSlugs: {len(trade_slugs)}")
    print(f"taker.outcomeSide present (YES/NO): {has_outcome}/{trade_n}   taker.intent present (BUY_LONG/SELL_SHORT): {has_intent}/{trade_n}")
    print(f"maker field combos (top): {maker_fields.most_common(3)}")
    print(f"taker field combos (top): {taker_fields.most_common(3)}")

    # sample a trade with full taker info for the report
    for line, p in _iter_lines(trade_files):
        outer, inner, cj = _parse(line)
        if outer is None or inner is None:
            continue
        tr = (inner.get("payload") or {}).get("trade") or {}
        if tr and (tr.get("taker") or {}).get("intent") not in (None, "ORDER_INTENT_UNDEFINED"):
            print(f"\nsample trade (taker intent):")
            print(json.dumps(tr, indent=2)[:1200])
            break

    print("\n=== audit done ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())