#!/usr/bin/env python3
"""Measure R(h) + fill-vs-quote race for scheduled gov releases from collected data.

Usage:
    python scripts/measure_reactions.py --market-slug <slug> [--event-id <id>]

Replays the book and trade raw stores once, then measures each release's
reaction window. Reports INSUFFICIENT_DATA for releases with no collected data
yet (e.g. future releases).
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.gov_releases import load_gov_releases  # noqa: E402
from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.us.adapter import amount_to_decimal  # noqa: E402
from polyalpha.us.fills import parse_us_trade  # noqa: E402
from polyalpha.us.quote_race import Quote  # noqa: E402
from polyalpha.us.reaction import measure_reaction  # noqa: E402


def _ts(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, UTC)


def _load_quotes(raw_dir: str, market_slug: str) -> list[Quote]:
    quotes = []
    for r in RawStore(raw_dir).replay():
        if r.source != "polymarket_us_retail_book":
            continue
        md = r.payload.get("marketData", r.payload) or {}
        if md.get("marketSlug") != market_slug:
            continue
        bids, offers = md.get("bids") or [], md.get("offers") or []
        if not bids or not offers:
            continue
        quotes.append(
            Quote(market_slug, _ts(r.received_at_ns),
                  amount_to_decimal(bids[0]["px"]), amount_to_decimal(offers[0]["px"]))
        )
    quotes.sort(key=lambda q: q.timestamp)
    return quotes


def _load_fills(raw_dir: str, market_slug: str) -> list:
    out = []
    for r in RawStore(raw_dir).replay():
        if r.source != "polymarket_us_retail_trade":
            continue
        try:
            f = parse_us_trade(r.payload, _ts(r.received_at_ns))
        except (ValueError, KeyError):
            continue
        if f.market_slug == market_slug:
            out.append(f)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure gov-release reactions")
    parser.add_argument("--schedule", default=str(ROOT / "config" / "gov_releases.json"))
    parser.add_argument("--book-raw", default="data/us/retail/raw")
    parser.add_argument("--trade-raw", default="data/us/trade/raw")
    parser.add_argument("--market-slug", required=True)
    parser.add_argument("--event-id", default=None)
    parser.add_argument("--pre-seconds", type=int, default=600)
    parser.add_argument("--stable-seconds", type=int, default=1800)
    args = parser.parse_args()

    source = load_gov_releases(args.schedule)
    now = datetime.now(UTC)
    if args.event_id:
        rel = source.by_id(args.event_id)
        releases = [rel] if rel else []
    else:
        recent = source.releases_between(now - timedelta(days=2), now)
        releases = recent or source.upcoming(now)

    quotes = _load_quotes(args.book_raw, args.market_slug)
    fills = _load_fills(args.trade_raw, args.market_slug)

    for rel in releases:
        start = rel.scheduled_at - timedelta(seconds=args.pre_seconds)
        end = rel.scheduled_at + timedelta(seconds=args.stable_seconds)
        w_quotes = [q for q in quotes if start <= q.timestamp <= end]
        w_fills = [f for f in fills if start <= f.trade_time <= end]
        if not w_quotes:
            print(json.dumps({
                "event_id": rel.event_id,
                "status": "INSUFFICIENT_DATA",
                "note": "no book snapshots in window (release future, or market not tracked)",
            }))
            continue
        print(json.dumps(
            measure_reaction(rel, w_quotes, w_fills, stable_seconds=args.stable_seconds),
            indent=2,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
