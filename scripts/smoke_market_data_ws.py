#!/usr/bin/env python3
"""Live smoke test for the Polymarket US market-data WebSocket.

Measures the feed's actual behavior (cadence, venue->receipt latency, transactTime
monotonicity, crossed books, BBO-vs-REST consistency, reconnect/stale handling)
so sub-2s horizons are justified by measurement, not assumption.

Usage:
    python scripts/smoke_market_data_ws.py --duration 60 --limit 25
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.us.adapter import amount_to_decimal  # noqa: E402
from polyalpha.us.execution import UsRetailAuth  # noqa: E402
from polyalpha.us.market_data_ws import MarketDataStream, parse_market_data  # noqa: E402
from polyalpha.us.rest import PublicUsClient  # noqa: E402


def _load_env(path: Path) -> None:
    import os
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


async def _run_once(ws, stream, subscribe, stats, deadline):
    await ws.send(json.dumps(subscribe))
    async for message in ws:
        if time.monotonic() >= deadline:
            return
        wall = datetime.now(UTC)
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        stats["messages_received"] += 1
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            stats["raw_frames_lost"] += 1
            continue
        if "heartbeat" in payload:
            stream.on_heartbeat(wall)
            continue
        update = parse_market_data(payload)
        if update is None:
            continue
        stream.ingest(payload, wall)

        if update.kind == "L2":
            stats["book_updates"] += 1
            if update.transact_time is not None:
                lat = (wall - update.transact_time).total_seconds() * 1000
                stats["venue_to_receipt_latency_ms"].append(lat)
            if stats["last_transact"] is not None and update.transact_time is not None:
                if update.transact_time < stats["last_transact"]:
                    stats["non_monotonic_transact"] += 1
            if update.transact_time is not None:
                stats["last_transact"] = update.transact_time
            prev = stats["last_update_at"]
            if prev is not None:
                stats["interarrival_ms"].append((wall - prev).total_seconds() * 1000)
            stats["last_update_at"] = wall
            if update.bids and update.offers:
                best_bid = update.bids[0][0]
                best_ask = update.offers[0][0]
                if best_bid >= best_ask:
                    stats["crossed_books"] += 1
                for price, qty in update.bids + update.offers:
                    if qty < 0:
                        stats["negative_qty"] += 1
            # Keep the latest BBO per slug for the REST cross-check.
            if update.bids and update.offers:
                stats["latest_bbo"][update.market_slug] = (update.bids[0][0], update.offers[0][0])
        elif update.kind == "BBO":
            stats["bbo_updates"] += 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the market-data WS")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    _load_env(Path(args.env))
    import os
    auth = UsRetailAuth(os.environ["POLYMARKET_US_ACCESS_KEY"], os.environ["POLYMARKET_US_SECRET"])
    client = PublicUsClient(timeout=20, attempts=2)
    raw, _ = client.markets({"limit": args.limit, "closed": "false"})
    slugs = [m["slug"] for m in raw.get("markets", []) if m.get("slug")]

    headers = auth.headers("GET", "/v1/ws/markets", str(int(time.time() * 1000)))
    subscribe = {"subscribe": {"requestId": "smoke-md", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA", "marketSlugs": slugs}}

    stream = MarketDataStream()
    stats = {
        "messages_received": 0, "book_updates": 0, "bbo_updates": 0,
        "interarrival_ms": [], "venue_to_receipt_latency_ms": [],
        "crossed_books": 0, "negative_qty": 0, "raw_frames_lost": 0,
        "non_monotonic_transact": 0, "reconnects": 0, "latest_bbo": {},
        "last_update_at": None, "last_transact": None,
    }

    import websockets
    started = time.monotonic()

    # Session 1.
    async with websockets.connect("wss://api.polymarket.us/v1/ws/markets", additional_headers=headers, open_timeout=15) as ws:
        stream.on_reconnect("session-1")
        await _run_once(ws, stream, subscribe, stats, time.monotonic() + args.duration)

    # Session 2 (forced reconnect) -> stale, then rebaseline.
    stats["reconnects"] += 1
    async with websockets.connect("wss://api.polymarket.us/v1/ws/markets", additional_headers=headers, open_timeout=15) as ws:
        stream.on_reconnect("session-2")
        stale_before = list(stream.stale_slugs)
        await _run_once(ws, stream, subscribe, stats, time.monotonic() + 15)
        rebaselined = [s for s in stale_before if s in stream.books and not stream.books[s].stale]

    session_duration = round(time.monotonic() - started, 2)

    # BBO (from WS L2) vs REST snapshot.
    book_mismatches = 0
    checks = 0
    for slug, (ws_bid, ws_ask) in list(stats["latest_bbo"].items())[:5]:
        try:
            rest, _ = client.market_book(slug)
            md = rest.get("marketData", rest)
            rb = amount_to_decimal((md.get("bids") or [{}])[0].get("px"))
            ra = amount_to_decimal((md.get("offers") or [{}])[0].get("px"))
            if rb is not None and ra is not None:
                checks += 1
                if abs(ws_bid - rb) > 0.02 or abs(ws_ask - ra) > 0.02:
                    book_mismatches += 1
        except Exception:
            pass

    ia = np.array(stats["interarrival_ms"]) if stats["interarrival_ms"] else np.array([0.0])
    v2r = np.array(stats["venue_to_receipt_latency_ms"]) if stats["venue_to_receipt_latency_ms"] else np.array([0.0])

    report = {
        "session_duration": session_duration,
        "messages_received": stats["messages_received"],
        "book_updates": stats["book_updates"],
        "bbo_updates": stats["bbo_updates"],
        "trades_observed": 0,  # market-data subscription carries no trades
        "median_interarrival_ms": round(float(np.median(ia)), 2),
        "p95_interarrival_ms": round(float(np.percentile(ia, 95)), 2),
        "p99_interarrival_ms": round(float(np.percentile(ia, 99)), 2),
        "venue_to_receipt_latency_ms": round(float(np.median(v2r)), 2),
        "sequence_gaps": 0,  # venue exposes no sequence number
        "reconnects": stats["reconnects"],
        "rest_rebaselines": len(rebaselined),
        "book_mismatches": book_mismatches,
        "bbo_checks": checks,
        "crossed_books": stats["crossed_books"],
        "negative_qty_levels": stats["negative_qty"],
        "non_monotonic_transact": stats["non_monotonic_transact"],
        "raw_frames_lost": stats["raw_frames_lost"],
        "stale_before_rebaseline": len(stale_before),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
