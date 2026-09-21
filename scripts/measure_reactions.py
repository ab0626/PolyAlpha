#!/usr/bin/env python3
"""Measure R(h) + fill-vs-quote race for scheduled gov releases from collected data.

Usage:
    # auto-discover the relevant contracts for each release via /v1/search
    python scripts/measure_reactions.py --discover
    # measure one specific contract
    python scripts/measure_reactions.py --market-slug <slug>

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

from polyalpha.gov_releases import discover_release_contracts, load_gov_releases  # noqa: E402
from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.us.adapter import amount_to_decimal  # noqa: E402
from polyalpha.us.fills import parse_us_trade  # noqa: E402
from polyalpha.us.quote_race import Quote  # noqa: E402
from polyalpha.us.reaction import measure_reaction  # noqa: E402
from polyalpha.us.rest import PublicUsClient  # noqa: E402


def _ts(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, UTC)


def _load_all_quotes(raw_dir: str) -> dict[str, list[Quote]]:
    out: dict[str, list[Quote]] = {}
    for r in RawStore(raw_dir).replay():
        if r.source != "polymarket_us_retail_book":
            continue
        md = r.payload.get("marketData", r.payload) or {}
        slug = md.get("marketSlug")
        if not slug:
            continue
        bids, offers = md.get("bids") or [], md.get("offers") or []
        if not bids or not offers:
            continue
        out.setdefault(slug, []).append(
            Quote(slug, _ts(r.received_at_ns),
                  amount_to_decimal(bids[0]["px"]), amount_to_decimal(offers[0]["px"]))
        )
    for qs in out.values():
        qs.sort(key=lambda q: q.timestamp)
    return out


def _load_all_fills(raw_dir: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in RawStore(raw_dir).replay():
        if r.source != "polymarket_us_retail_trade":
            continue
        try:
            f = parse_us_trade(r.payload, _ts(r.received_at_ns))
        except (ValueError, KeyError):
            continue
        if f.market_slug:
            out.setdefault(f.market_slug, []).append(f)
    return out


def _search_fn(client: PublicUsClient):
    def _f(term: str):
        raw, _ = client.search({"query": term, "status": "active", "limit": 20})
        return raw.get("events", [])
    return _f


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure gov-release reactions")
    parser.add_argument("--schedule", default=str(ROOT / "config" / "gov_releases.json"))
    parser.add_argument("--book-raw", default="data/us/retail/raw")
    parser.add_argument("--trade-raw", default="data/us/trade/raw")
    parser.add_argument("--market-slug", default=None)
    parser.add_argument("--discover", action="store_true", help="auto-discover contracts via /v1/search")
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

    quotes_by_slug = _load_all_quotes(args.book_raw)
    fills_by_slug = _load_all_fills(args.trade_raw)
    client = PublicUsClient(timeout=20, attempts=2) if args.discover else None

    for rel in releases:
        if args.market_slug:
            slugs = [args.market_slug]
        elif args.discover:
            slugs = discover_release_contracts(rel, _search_fn(client))
        else:
            slugs = []

        if not slugs:
            print(json.dumps({"event_id": rel.event_id, "status": "NO_CONTRACTS",
                              "note": "no --market-slug and no --discover (or nothing matched)"}))
            continue

        start = rel.scheduled_at - timedelta(seconds=args.pre_seconds)
        end = rel.scheduled_at + timedelta(seconds=args.stable_seconds)
        for slug in slugs:
            w_quotes = [q for q in quotes_by_slug.get(slug, []) if start <= q.timestamp <= end]
            w_fills = [f for f in fills_by_slug.get(slug, []) if start <= f.trade_time <= end]
            if not w_quotes:
                print(json.dumps({"event_id": rel.event_id, "market_slug": slug,
                                  "status": "INSUFFICIENT_DATA",
                                  "note": "no book snapshots in window (future release, or not tracked)"}))
                continue
            print(json.dumps(measure_reaction(rel, w_quotes, w_fills, stable_seconds=args.stable_seconds), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
