#!/usr/bin/env python3
"""Collect market metadata and order books from Polymarket.

Usage:
    python scripts/collect_markets.py --config config/base.toml --output data/markets.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polyalpha.collector import Collector
from polyalpha.config import load_config
from polyalpha.quality import Filter
from polyalpha.storage import Store
from polyalpha.transport import PublicHTTP


def main():
    parser = argparse.ArgumentParser(description="Collect Polymarket market data")
    parser.add_argument("--config", default="config/base.toml", help="Config file path")
    parser.add_argument("--output", default="data/markets.json", help="Output JSON path")
    parser.add_argument("--pages", type=int, default=5, help="Max pages to collect")
    parser.add_argument("--page-size", type=int, default=100, help="Page size")
    args = parser.parse_args()

    config = load_config(args.config)
    collection = config["collection"]

    transport = PublicHTTP(collection["timeout_seconds"], collection["attempts"])
    quality = Filter(**config["market_filter"])

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Store(str(output_path.with_suffix(".sqlite"))) as store:
        stats = Collector(transport, store, quality).collect(args.page_size, args.pages)

        # Export market summaries
        markets = []
        for receipt in store.replay(store._now(), "market"):
            markets.append(receipt.payload)

        with open(output_path, "w") as f:
            json.dump({"stats": stats, "markets": markets}, f, indent=2, default=str)

        print(f"Collected {len(markets)} markets to {output_path}")
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
