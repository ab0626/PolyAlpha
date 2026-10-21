#!/usr/bin/env python3
"""Build the Polymarket US fee-metadata manifest for the archived lineage.

Queries GET gateway.polymarket.us/v1/markets, extracts per-market
feeCoefficient/category/createdAt/etc., matches archived marketSlugs, hashes the
raw responses + manifest, and marks unmatched slugs UNKNOWN (never imputed).

Usage:
    .venv/Scripts/python.exe scripts/polymarket_fee_manifest.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.us.rest import PublicUsClient  # noqa: E402

MD_ROOT = ROOT / "data" / "us" / "market_data" / "raw"
TRADE_ROOT = ROOT / "data" / "us" / "trade"
OUT = ROOT / "data" / "us" / "polymarket_fee_manifest"


def _slugs(max_files: int = 150):
    slugs = set()
    md_files = sorted(MD_ROOT.rglob("polymarket_us_retail_market_data-*.jsonl"))[-max_files:]
    for p in md_files:
        op = gzip.open if p.suffix == ".gz" else open
        with op(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                    inn = json.loads(o["canonical_json"])
                    md = (inn.get("payload") or {}).get("marketData") or {}
                    if md.get("marketSlug"):
                        slugs.add(md["marketSlug"])
                except Exception:
                    continue
    trade_files = sorted(TRADE_ROOT.rglob("*.jsonl"))[-100:]
    for p in trade_files:
        op = gzip.open if p.suffix == ".gz" else open
        with op(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                    inn = json.loads(o["canonical_json"])
                    tr = (inn.get("payload") or {}).get("trade") or {}
                    if tr.get("marketSlug"):
                        slugs.add(tr["marketSlug"])
                except Exception:
                    continue
    return slugs


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-files", type=int, default=150)
    args = ap.parse_args()
    slugs = _slugs(args.max_files)
    client = PublicUsClient()

    # fetch all markets (paginate by cursor if present)
    all_markets = []
    cursor = None
    for _ in range(200):
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        body, _ = client.markets(params=params)
        markets = body.get("markets", []) if isinstance(body, dict) else []
        if not markets:
            break
        all_markets.extend(markets)
        # detect pagination
        nxt = body.get("nextCursor") or body.get("cursor")
        if not nxt or len(markets) < 1000:
            break
        cursor = nxt
    print(f"API markets fetched: {len(all_markets)}   archived slugs: {len(slugs)}")

    raw_json = json.dumps(all_markets, sort_keys=True)
    raw_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

    manifest = {}
    fee_dist = Counter()
    for m in all_markets:
        slug = m.get("slug") or m.get("marketSlug")
        if not slug:
            continue
        fc = m.get("feeCoefficient")
        manifest[slug] = {
            "market_id": m.get("id"),
            "category": m.get("category"),
            "createdAt": m.get("createdAt"),
            "startDate": m.get("startDate"),
            "feeCoefficient": fc,
            "marketType": m.get("marketType"),
            "comboEnabled": m.get("comboEnabled"),
        }
        if slug in slugs:
            fee_dist[fc if fc is not None else "MISSING"] += 1

    matched = sum(1 for s in slugs if s in manifest)
    unknown = sorted(s for s in slugs if s not in manifest)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "raw_markets.json").write_text(raw_json + "\n", encoding="utf-8")
    manifest_doc = {
        "retrieved_at": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "raw_metadata_sha256": raw_sha,
        "api_market_count": len(all_markets),
        "archived_slug_count": len(slugs),
        "matched": matched,
        "unknown_count": len(unknown),
        "feeCoefficient_distribution": dict(fee_dist),
        "manifest": manifest,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_sha = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()

    print(f"matched: {matched}/{len(slugs)}   UNKNOWN: {len(unknown)}")
    print(f"feeCoefficient distribution (matched archived): {dict(fee_dist)}")
    print(f"raw_metadata_sha256: {raw_sha}")
    print(f"manifest_sha256: {manifest_sha}")
    print(f"UNKNOWN slugs (first 20): {unknown[:20]}")
    print(f"manifest -> {OUT / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())