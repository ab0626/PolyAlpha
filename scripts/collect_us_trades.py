#!/usr/bin/env python3
"""Sustained US retail trade-stream collector (authenticated WebSocket).

Collects the market-wide trade tape (explicit maker/taker side+intent) into the
raw store under source `polymarket_us_retail_trade`. Research lineage only; no
execution. Auth is read from POLYMARKET_US_ACCESS_KEY / POLYMARKET_US_SECRET.

Usage:
    python scripts/collect_us_trades.py --duration 3600 --limit 100
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


def main() -> int:
    parser = argparse.ArgumentParser(description="US retail trade-stream collector")
    parser.add_argument("--raw-dir", default="data/us/trade/raw")
    parser.add_argument("--duration", type=float, default=900.0,
                        help="Seconds to run (exits for universe rediscovery)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Active markets to subscribe to")
    parser.add_argument("--schedule", default=str(ROOT / "config" / "gov_releases.json"))
    parser.add_argument("--mapping", default="data/us/release_mapping.jsonl")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    args = parser.parse_args()

    _load_env(Path(args.env))

    from polyalpha.us.release_coverage import collector_universe
    from polyalpha.us.rest import PublicUsClient
    from polyalpha.us.trade_stream import run_collector

    client = PublicUsClient(timeout=20, attempts=3)
    _activity, _required, required_slugs, universe = collector_universe(
        client, args.schedule, args.mapping, limit=args.limit
    )
    print(f"universe={len(universe)} (required={len(required_slugs)})", flush=True)

    # Persist the subscribed universe so coverage checks the subscription
    # invariant, not sparse trade observation.
    universe_path = Path("data/us/logs/trade-universe.json")
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    universe_path.write_text(
        json.dumps({
            "ts": datetime.now(UTC).isoformat(),
            "slugs": universe,
            "required_slugs": required_slugs,
        }, sort_keys=True),
        encoding="utf-8",
    )

    stats = run_collector(args.raw_dir, markets=universe, duration=args.duration)
    print("collection stats:", stats, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
