#!/usr/bin/env python3
"""End-to-end information-propagation pipeline (append-only, restart-safe).

Flow: authoritative release -> EventShock -> release->contract mapping ->
pre-event/WS observations -> ShockMarketReaction (R(h), t_x) -> append to the
evidence store with deterministic ids. Cross-market propagation (Z_lag) is
computed by ``polyalpha.integration.compute_propagation_evidence`` once an event
graph and historical samples exist; edge_exec is UNAVAILABLE until the
reachable-VWAP/fee layer lands.

Usage:
    python scripts/run_info_pipeline.py --evidence data/us/evidence.jsonl
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.event_shock import (  # noqa: E402
    EventShock,
    MarketLink,
    Relationship,
    measure_shock_reaction,
)
from polyalpha.gov_releases import discover_release_contracts, load_gov_releases  # noqa: E402
from polyalpha.integration import EvidenceStore, reaction_id, shock_id  # noqa: E402
from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.us.adapter import amount_to_decimal  # noqa: E402
from polyalpha.us.fills import parse_us_trade  # noqa: E402
from polyalpha.us.quote_race import Quote  # noqa: E402
from polyalpha.us.rest import PublicUsClient  # noqa: E402


def _ts(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, UTC)


def _load_quotes(raw_dir: str) -> dict[str, list[Quote]]:
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


def _load_fills(raw_dir: str) -> dict[str, list]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the information-propagation pipeline")
    parser.add_argument("--schedule", default=str(ROOT / "config" / "gov_releases.json"))
    parser.add_argument("--book-raw", default="data/us/retail/raw")
    parser.add_argument("--trade-raw", default="data/us/trade/raw")
    parser.add_argument("--evidence", default="data/us/evidence.jsonl")
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
        releases = source.releases_between(now - timedelta(days=2), now) or source.upcoming(now)

    store = EvidenceStore(args.evidence)
    quotes_by_slug = _load_quotes(args.book_raw)
    fills_by_slug = _load_fills(args.trade_raw)
    client = PublicUsClient(timeout=20, attempts=2)

    n_shocks = n_reactions = n_edges = 0
    for rel in releases:
        sid = shock_id(rel.event_id, rel.scheduled_at)
        slugs = discover_release_contracts(
            rel, lambda term: client.search({"query": term, "status": "active", "limit": 20})[0].get("events", [])
        )
        links = [MarketLink(f"us:{s}", Relationship.PRIMARY, 1.0) for s in slugs]
        shock = EventShock(
            shock_id=sid, event_id=rel.event_id, category=rel.category,
            source="official", source_authority=1.0, primary_source=True,
            first_public_at=rel.scheduled_at,
            first_received_at=rel.scheduled_at,
            first_processed_at=rel.scheduled_at,
            affected_markets=tuple(links),
        )
        store.append({
            "id": sid, "kind": "SHOCK", "event_id": rel.event_id,
            "scheduled_at": rel.scheduled_at.isoformat(), "name": rel.name,
            "affected_markets": [link.market_id for link in links],
        })
        n_shocks += 1

        start = rel.scheduled_at - timedelta(seconds=args.pre_seconds)
        end = rel.scheduled_at + timedelta(seconds=args.stable_seconds)
        for slug, link in zip(slugs, links):
            mid = link.market_id
            rid = reaction_id(sid, mid)
            quotes = [q for q in quotes_by_slug.get(slug, []) if start <= q.timestamp <= end]
            fills = [f for f in fills_by_slug.get(slug, []) if start <= f.trade_time <= end]
            if not quotes:
                store.append({"id": rid, "kind": "REACTION", "shock_id": sid,
                              "market_id": mid, "status": "INSUFFICIENT_DATA"})
                continue
            reaction = measure_shock_reaction(shock, mid, quotes, fills,
                                              stable_seconds=args.stable_seconds,
                                              feed_label="REST_2S")
            store.append({"id": rid, "kind": "REACTION", **reaction.summary()})
            n_reactions += 1
        # Cross-market edges need the event graph + historical samples; not
        # computed until those exist (edge_exec stays UNAVAILABLE).
        n_edges += 0

    print(json.dumps({
        "n_shocks": n_shocks,
        "n_primary_reactions": n_reactions,
        "n_cross_market_edges": n_edges,
        "n_independent_clusters": 0,  # populated when reactions accumulate
        "evidence_file": str(store.path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
