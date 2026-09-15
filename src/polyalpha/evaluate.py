"""Fit and evaluate calibration folds from a versioned, explicit outcome CSV."""

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from .calibration import Observation, walk_forward
from .comparison import summarize_folds
from .monitoring import code_provenance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--first-test", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--method", choices=["isotonic", "platt"], default="isotonic")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.csv, encoding="utf-8-sig", newline="") as source:
        rows = [
            Observation(
                r["market_id"],
                r["cluster"],
                datetime.fromisoformat(r["predicted_at"]),
                datetime.fromisoformat(r["label_known_at"]) if r["label_known_at"] else None,
                float(r["probability"]),
                int(r["outcome"]) if r["outcome"] else None,
            )
            for r in csv.DictReader(source)
        ]
    folds = walk_forward(
        rows,
        datetime.fromisoformat(args.first_test),
        datetime.fromisoformat(args.end),
        timedelta(days=args.window_days),
        args.method,
    )
    with Path(args.output).open("x", encoding="utf-8") as destination:
        import hashlib

        json.dump(
            dict(
                method=args.method,
                folds=folds,
                aggregate=summarize_folds(folds),
                input_sha256=hashlib.sha256(Path(args.csv).read_bytes()).hexdigest(),
                provenance={
                    **code_provenance(),
                    "model_version": f"calibration-{args.method}",
                    "feature_version": "provided-probability-csv-v1",
                },
                first_test=args.first_test,
                end=args.end,
                window_days=args.window_days,
            ),
            destination,
            indent=2,
        )


if __name__ == "__main__":
    main()
