#!/usr/bin/env python3
"""Collect primary-source news (observation-only, no API key required).

Runs the provider-neutral NewsCollector against the RSS primary-source feeds in
the config. Accumulates raw news into a SEPARATE lineage (data/news/raw); no
model, signal, or market path consumes it during the v0.4 freeze.

Usage:
    python scripts/collect_news.py --feeds config/news_feeds.json
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.news import NewsCollector  # noqa: E402
from polyalpha.news_rss import RssNewsProvider  # noqa: E402
from polyalpha.rawstore import RawStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect primary-source news")
    parser.add_argument("--feeds", default=str(ROOT / "config" / "news_feeds.json"))
    parser.add_argument("--raw-dir", default="data/news/raw")
    parser.add_argument("--watermark", default="data/news/watermarks.json")
    args = parser.parse_args()

    data = json.loads(Path(args.feeds).read_text(encoding="utf-8"))
    providers = [
        RssNewsProvider(name=feed["name"], feed_urls=feed["urls"])
        for feed in data.get("feeds", [])
    ]
    if not providers:
        print("no feeds configured in", args.feeds, file=sys.stderr)
        return 1

    raw = RawStore(args.raw_dir, collector_version="v0.4.1-us-research-baseline")
    collector = NewsCollector(providers, raw, args.watermark)
    try:
        stats = collector.collect()
    finally:
        raw.close()
    print(json.dumps(stats, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
