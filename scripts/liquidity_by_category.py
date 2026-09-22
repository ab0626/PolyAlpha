#!/usr/bin/env python3
"""Descriptive, point-in-time liquidity report by frozen category stratum.

Answers "what is actually measurable/executable on Polymarket US?" — NOT a
ranking of what to trade. Per stratum: median spread, depth +/-1c/+/-5c, VWAP
impact(q), trades/hour, quote updates/hour, time-since-last-trade, and sample
support (N markets, N market-hours, N fills). No predictive interpretation.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.us.adapter import amount_to_decimal  # noqa: E402


def _qty(v) -> D:
    if isinstance(v, dict):
        v = v.get("value")
    return D(str(v))


def _stratum(slug: str, category: str | None, strata: dict) -> str:
    if category:
        for name, cfg in strata.items():
            if category in cfg.get("categories", []):
                return name
    for name, cfg in strata.items():
        if any(slug.startswith(p) for p in cfg.get("slug_prefixes", [])):
            return name
    return "other"


def _vwap_buy(offers: list[tuple[D, D]], q: D) -> D | None:
    remaining = q
    cost = D(0)
    for price, size in offers:
        fill = min(size, remaining)
        cost += price * fill
        remaining -= fill
        if remaining <= 0:
            return cost / q
    return None


def _book_metrics(bids, offers, q):
    if not bids or not offers:
        return None
    best_bid = bids[0][0]
    best_ask = offers[0][0]
    spread = best_ask - best_bid
    depth_1c = sum(s for p, s in bids if p >= best_bid - D("0.01")) + sum(s for p, s in offers if p <= best_ask + D("0.01"))
    depth_5c = sum(s for p, s in bids if p >= best_bid - D("0.05")) + sum(s for p, s in offers if p <= best_ask + D("0.05"))
    vwap = _vwap_buy(offers, q)
    mid = (best_bid + best_ask) / D(2)
    impact = (vwap - mid) if vwap is not None else None
    return spread, depth_1c, depth_5c, impact


def main() -> int:
    parser = argparse.ArgumentParser(description="Liquidity by category")
    parser.add_argument("--strata", default=str(ROOT / "config" / "liquidity_strata.json"))
    parser.add_argument("--book-raw", default="data/us/retail/raw")
    parser.add_argument("--trade-raw", default="data/us/trade/raw")
    args = parser.parse_args()

    cfg = json.loads(Path(args.strata).read_text(encoding="utf-8"))
    strata = cfg["category_strata"]
    q = D(str(cfg["vwap_q_shares"]))

    # slug -> category from discovery metadata (where captured).
    meta: dict[str, str] = {}
    for r in RawStore(args.book_raw).replay():
        if r.source == "polymarket_us_retail_markets":
            ms = r.payload.get("markets", []) if isinstance(r.payload, dict) else []
            for m in ms:
                if isinstance(m, dict) and m.get("slug"):
                    meta[m["slug"]] = m.get("category")

    per: dict[str, dict] = defaultdict(lambda: {
        "spreads": [], "depth_1c": [], "depth_5c": [], "impacts": [],
        "quotes": 0, "first": None, "last": None,
        "trades": 0, "last_trade": None, "slugs": set(),
    })

    for r in RawStore(args.book_raw).replay():
        if r.source != "polymarket_us_retail_book":
            continue
        md = r.payload.get("marketData", r.payload) or {}
        slug = md.get("marketSlug")
        if not slug:
            continue
        bids = [(amount_to_decimal(level.get("px")), _qty(level.get("qty"))) for level in md.get("bids") or []]
        offers = [(amount_to_decimal(level.get("px")), _qty(level.get("qty"))) for level in md.get("offers") or []]
        m = _book_metrics(bids, offers, q)
        if m is None:
            continue
        stratum = _stratum(slug, meta.get(slug), strata)
        p = per[stratum]
        p["slugs"].add(slug)
        p["spreads"].append(float(m[0]))
        p["depth_1c"].append(float(m[1]))
        p["depth_5c"].append(float(m[2]))
        if m[3] is not None:
            p["impacts"].append(float(m[3]))
        p["quotes"] += 1
        ts = datetime.fromtimestamp(r.received_at_ns / 1e9, UTC)
        p["first"] = ts if p["first"] is None else min(p["first"], ts)
        p["last"] = ts if p["last"] is None else max(p["last"], ts)

    for r in RawStore(args.trade_raw).replay():
        if r.source != "polymarket_us_retail_trade":
            continue
        tr = r.payload.get("trade", r.payload) if isinstance(r.payload, dict) else {}
        slug = tr.get("marketSlug")
        if not slug:
            continue
        stratum = _stratum(slug, meta.get(slug), strata)
        p = per[stratum]
        p["slugs"].add(slug)
        p["trades"] += 1
        tt = tr.get("tradeTime")
        if tt:
            try:
                t = datetime.fromisoformat(tt.replace("Z", "+00:00"))
                p["last_trade"] = t if p["last_trade"] is None else max(p["last_trade"], t)
            except ValueError:
                pass

    now = datetime.now(UTC)
    out = {}
    for name in sorted(per):
        p = per[name]
        hours = (p["last"] - p["first"]).total_seconds() / 3600 if p["first"] and p["last"] else 0.0
        out[name] = {
            "median_spread": round(statistics.median(p["spreads"]), 4) if p["spreads"] else None,
            "depth_1c_median": round(statistics.median(p["depth_1c"]), 1) if p["depth_1c"] else None,
            "depth_5c_median": round(statistics.median(p["depth_5c"]), 1) if p["depth_5c"] else None,
            "vwap_impact_median": round(statistics.median(p["impacts"]), 4) if p["impacts"] else None,
            "trades_per_hour": round(p["trades"] / hours, 2) if hours > 0 else None,
            "quote_updates_per_hour": round(p["quotes"] / hours, 1) if hours > 0 else None,
            "time_since_last_trade_seconds": round((now - p["last_trade"]).total_seconds(), 0) if p["last_trade"] else None,
            "n_markets": len(p["slugs"]),
            "n_market_hours": round(hours, 1),
            "n_fills": p["trades"],
        }

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
