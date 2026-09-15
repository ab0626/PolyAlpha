#!/usr/bin/env python3
"""Collect order books for tracked markets.

Usage:
    python scripts/collect_books.py --config config/base.toml --database data/polyalpha.sqlite
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polyalpha.collector import Collector
from polyalpha.config import load_config
from polyalpha.quality import Filter
from polyalpha.storage import Store
from polyalpha.transport import PublicHTTP


def main():
    parser = argparse.ArgumentParser(description="Collect order books")
    parser.add_argument("--config", default="config/base.toml")
    parser.add_argument("--database", default="data/polyalpha.sqlite")
    parser.add_argument("--pages", type=int, default=10)
    args = parser.parse_args()

    config = load_config(args.config)
    collection = config["collection"]

    transport = PublicHTTP(collection["timeout_seconds"], collection["attempts"])
    quality = Filter(**config["market_filter"])

    with Store(args.database) as store:
        stats = Collector(transport, store, quality).collect(collection["page_size"], args.pages)

        # Count books
        book_count = sum(1 for _ in store.replay(datetime.now(UTC), "book"))

        print(f"Collected {book_count} order books")
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
