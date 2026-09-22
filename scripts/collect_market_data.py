#!/usr/bin/env python3
"""Durable Polymarket US market-data WebSocket collector (~100ms L2/BBO).

Production data acquisition (not feature work): auto-connect, reconnect safely,
re-baseline after session breaks, preserve raw wire data, and write a health
artifact so staleness is externally observable.

Universe = U_activity  U  U_release_required (release contracts are never
displaced by activity ranking). Runs for a bounded duration and exits so the
launcher loop re-discovers the universe periodically (additive via the persisted
point-in-time mapping).

Usage:
    python scripts/collect_market_data.py --limit 50 --duration 900
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.us.market_data_ws import MarketDataWsCollector  # noqa: E402
from polyalpha.us.release_coverage import collector_universe  # noqa: E402
from polyalpha.us.rest import PublicUsClient  # noqa: E402


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Durable market-data WS collector")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--raw-dir", default="data/us/market_data/raw")
    parser.add_argument("--schedule", default=str(ROOT / "config" / "gov_releases.json"))
    parser.add_argument("--mapping", default="data/us/release_mapping.jsonl")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--duration", type=float, default=900.0)
    parser.add_argument("--status", default="data/us/logs/market-data-status.json")
    parser.add_argument("--status-interval", type=float, default=15.0)
    args = parser.parse_args()

    _load_env(Path(args.env))
    client = PublicUsClient(timeout=20, attempts=3)
    activity, required, required_slugs, universe = collector_universe(
        client, args.schedule, args.mapping, limit=args.limit
    )
    required_collecting = sorted(set(required_slugs) & set(universe))
    required_missing = sorted(set(required_slugs) - set(universe))
    print(f"universe={len(universe)} (activity={len(activity)} required={len(required_slugs)})", flush=True)

    raw = RawStore(args.raw_dir, collector_version="v0.4.1-us-research-baseline")
    collector = MarketDataWsCollector.from_env(raw)

    status_path = Path(args.status)
    started = time.monotonic()

    def _write_status():
        status = {
            "ts": datetime.now(UTC).isoformat(),
            "uptime_seconds": round(time.monotonic() - started, 1),
            "updates": collector.stats["updates"],
            "reconnects": collector.stats["reconnects"],
            "errors": collector.stats["errors"],
            "session_id": collector.stream.session_id,
            "stale_slugs": len(collector.stream.stale_slugs),
            "last_heartbeat": (
                collector.stream.last_heartbeat_at.isoformat()
                if collector.stream.last_heartbeat_at else None
            ),
            "required_release_markets": len(required_slugs),
            "required_markets_collecting": len(required_collecting),
            "required_markets_missing": len(required_missing),
        }
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")

    async def status_writer():
        while True:
            await asyncio.sleep(args.status_interval)
            try:
                _write_status()
            except OSError:
                pass

    writer = asyncio.create_task(status_writer())
    try:
        stats = await collector.run(universe, lite=False, duration=args.duration)
        print("collector exited:", stats, flush=True)
        _write_status()
        return 0
    finally:
        writer.cancel()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
