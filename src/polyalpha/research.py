"""Research commands; reports are written to local files, never trading endpoints."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from .cli import settings
from .config import make_engine
from .storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/research.sqlite3")
    parser.add_argument("--config", default="config/base.toml")
    parser.add_argument("--clusters", required=True)
    parser.add_argument("--output", default="data/paper-report.json")
    parser.add_argument("--as-of", required=True, help="ISO timestamp with timezone")
    args = parser.parse_args()
    cutoff = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    with open(args.clusters, encoding="utf-8-sig") as source:
        assignments = json.load(source)
    config, quality = settings(args.config)
    with Store(args.database) as store:
        report = make_engine(config, assignments, quality).run(store.replay(cutoff))
    from .monitoring import code_provenance

    report["provenance"] = code_provenance()
    report["configuration"] = config
    report["cluster_assignments"] = assignments
    report["cutoff"] = cutoff.isoformat()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Refuse accidental replacement of earlier experiment artifacts.
    with target.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, default=str)
    print(
        json.dumps(
            {
                "report": str(target.resolve()),
                "fills": report["fills"],
                "status": report["status"],
            }
        )
    )


if __name__ == "__main__":
    main()
