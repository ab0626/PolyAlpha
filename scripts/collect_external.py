#!/usr/bin/env python3
"""Collect shadow-external-v0 observations (observation-only).

Builds the Phase-1 providers (FRED, EIA, NWS, SEC, Fed RSS, GDELT) from a JSON
config and runs the ExternalCollector into a SEPARATE raw lineage. No model,
signal, or v0.4 path consumes this. Keys (FRED/EIA) come from environment.

Usage:
    python scripts/collect_external.py --providers config/external_providers.json
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.shadow import ExternalCollector, SourceClass  # noqa: E402
from polyalpha.shadow.sources import (  # noqa: E402
    EiaProvider,
    FredProvider,
    GdeltProvider,
    NwsProvider,
    RssProvider,
    SecProvider,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect shadow external observations")
    parser.add_argument("--providers", default=str(ROOT / "config" / "external_providers.json"))
    parser.add_argument("--raw-dir", default="data/shadow/raw")
    parser.add_argument("--watermark", default="data/shadow/watermarks.json")
    args = parser.parse_args()

    cfg = json.loads(Path(args.providers).read_text(encoding="utf-8"))
    providers = []
    for p in cfg.get("providers", []):
        kind = p["kind"]
        if kind == "fred":
            providers.append(FredProvider(p["series_ids"]))
        elif kind == "eia":
            providers.append(EiaProvider(p["routes"]))
        elif kind == "nws":
            providers.append(NwsProvider(p.get("state_codes")))
        elif kind == "sec":
            providers.append(SecProvider(p["ciks"]))
        elif kind == "rss":
            providers.append(RssProvider(p["name"], SourceClass[p["source_class"]], p["urls"]))
        elif kind == "gdelt":
            providers.append(GdeltProvider(p["query"]))
    if not providers:
        print("no providers configured", file=sys.stderr)
        return 1

    raw = RawStore(args.raw_dir, collector_version="shadow-external-v0")
    collector = ExternalCollector(providers, raw, args.watermark)
    try:
        stats = collector.collect()
    finally:
        raw.close()
    print(json.dumps(stats, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
