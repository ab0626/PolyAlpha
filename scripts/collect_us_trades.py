#!/usr/bin/env python3
"""Sustained US retail trade-stream collector (authenticated WebSocket).

Collects the market-wide trade tape (explicit maker/taker side+intent) into the
raw store under source `polymarket_us_retail_trade`. Research lineage only; no
execution. Auth is read from POLYMARKET_US_ACCESS_KEY / POLYMARKET_US_SECRET.

Usage:
    python scripts/collect_us_trades.py --duration 3600 --limit 100
"""

import argparse
import os
import sys
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
    parser.add_argument("--duration", type=float, default=None,
                        help="Seconds to run (None = until cancelled)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Active markets to subscribe to")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    args = parser.parse_args()

    _load_env(Path(args.env))

    from polyalpha.us.rest import PublicUsClient
    from polyalpha.us.trade_stream import run_collector

    client = PublicUsClient(timeout=20, attempts=3)
    raw, _ = client.markets({"limit": args.limit, "closed": "false"})
    slugs = [m["slug"] for m in raw.get("markets", []) if m.get("slug")]
    print(f"subscribing to {len(slugs)} active markets", flush=True)

    stats = run_collector(args.raw_dir, markets=slugs, duration=args.duration)
    print("collection stats:", stats, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
