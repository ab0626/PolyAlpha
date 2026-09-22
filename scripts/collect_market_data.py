#!/usr/bin/env python3
"""Durable Polymarket US market-data WebSocket collector (~100ms L2/BBO).

Production data acquisition (not feature work): auto-connect, reconnect safely,
re-baseline after session breaks, preserve raw wire data, and write a health
artifact so staleness is externally observable.

Usage:
    python scripts/collect_market_data.py --limit 50
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
from polyalpha.us.rest import PublicUsClient  # noqa: E402


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


def _write_status(collector: MarketDataWsCollector, status_path: Path, started: float) -> None:
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
    }
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Durable market-data WS collector")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--raw-dir", default="data/us/market_data/raw")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--status", default="data/us/logs/market-data-status.json")
    parser.add_argument("--status-interval", type=float, default=15.0)
    args = parser.parse_args()

    _load_env(Path(args.env))
    raw = RawStore(args.raw_dir, collector_version="v0.4.1-us-research-baseline")
    collector = MarketDataWsCollector.from_env(raw)

    client = PublicUsClient(timeout=20, attempts=3)
    body, _ = client.markets({"limit": args.limit, "closed": "false"})
    slugs = [m["slug"] for m in body.get("markets", []) if m.get("slug")]
    print(f"subscribing to {len(slugs)} active markets", flush=True)

    status_path = Path(args.status)
    started = time.monotonic()

    async def status_writer():
        while True:
            await asyncio.sleep(args.status_interval)
            try:
                _write_status(collector, status_path, started)
            except OSError:
                pass

    writer = asyncio.create_task(status_writer())
    try:
        stats = await collector.run(slugs, lite=False, duration=None)
        print("collector exited:", stats, flush=True)
        return 0
    finally:
        writer.cancel()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
