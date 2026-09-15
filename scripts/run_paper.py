#!/usr/bin/env python3
"""Run paper trading simulation.

Usage:
    python scripts/run_paper.py --config config/paper.yaml --database data/paper.sqlite
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polyalpha.backtest import Engine
from polyalpha.collector import Collector
from polyalpha.config import load_config
from polyalpha.quality import Filter
from polyalpha.storage import Store
from polyalpha.transport import PublicHTTP


def main():
    parser = argparse.ArgumentParser(description="Run paper trading")
    parser.add_argument("--config", default="config/paper.yaml")
    parser.add_argument("--database", default="data/paper.sqlite")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--clusters", default="config/clusters.json")
    args = parser.parse_args()

    config = load_config(args.config)
    collection = config["collection"]

    # Load clusters
    clusters_path = Path(args.clusters)
    if clusters_path.exists():
        with open(clusters_path) as f:
            clusters = json.load(f)
    else:
        clusters = {}

    transport = PublicHTTP(collection["timeout_seconds"], collection["attempts"])
    quality = Filter(**config["market_filter"])

    risk_config = config.get("risk", {})
    signal_config = config.get("signal", {})

    with Store(args.database) as store:
        for cycle in range(args.cycles):
            # Collect fresh data
            Collector(transport, store, quality).collect(
                collection["page_size"], collection["max_pages"]
            )

            # Run engine on all data
            engine = Engine(
                clusters=clusters,
                initial_cash=Decimal(str(risk_config.get("initial_cash", 10000))),
                min_edge=Decimal(str(signal_config.get("min_net_edge", 0.025))),
            )

            records = list(store.replay(datetime.now(UTC)))
            report = engine.run(records)

            # Print summary
            print(f"\n=== Cycle {cycle + 1}/{args.cycles} ===")
            print(f"Records processed: {report['input_records']}")
            print(f"Fills: {report['fills']}")
            print(f"Pending: {report['pending_unfilled']}")
            print(f"Cash: ${report['cash']}")
            print(f"Net PnL: {report.get('net_pnl', 'N/A')}")
            print(f"Max Drawdown: {report.get('max_drawdown', 'N/A')}")

            if cycle + 1 < args.cycles:
                interval = collection.get("interval_seconds", 300)
                print(f"Waiting {interval}s before next cycle...")
                time.sleep(interval)

    print("\nPaper trading complete.")


if __name__ == "__main__":
    main()
