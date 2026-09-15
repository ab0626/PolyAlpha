"""Bounded paper sessions reconstructed from immutable receipts on every cycle."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .cli import collect_cycle, settings
from .config import make_engine
from .storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/base.toml")
    parser.add_argument("--clusters", default="config/clusters.json")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--output-dir", default="data/paper")
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("cycles must be positive")
    config, quality = settings(args.config)
    with open(args.clusters, encoding="utf-8-sig") as source:
        assignments = json.load(source)
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for cycle in range(args.cycles):
        stats = collect_cycle(config, quality, refresh=True, resolutions=True)
        cutoff = datetime.now(UTC)
        with Store(config["collection"]["database"]) as store:
            # Replaying the same immutable history restores accounting, consumed
            # liquidity and circuit-breaker state without resubmitting any orders.
            report = make_engine(config, assignments, quality).run(store.replay(cutoff))
        from .monitoring import code_provenance

        report["provenance"] = code_provenance()
        report["configuration"] = config
        report["cluster_assignments"] = assignments
        report["cutoff"] = cutoff.isoformat()
        path = directory / f"{uuid4()}.json"
        with path.open("x", encoding="utf-8") as destination:
            json.dump(report, destination, indent=2, default=str)
        print(
            json.dumps(
                dict(
                    event="paper_cycle",
                    cycle=cycle + 1,
                    collection=stats,
                    report=str(path),
                    fills=report["fills"],
                )
            ),
            flush=True,
        )
        if cycle + 1 < args.cycles:
            time.sleep(config["collection"]["interval_seconds"])


if __name__ == "__main__":
    main()
