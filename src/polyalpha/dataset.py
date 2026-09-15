"""Export replay forecasts and their actual outcome availability into evaluation CSV."""

import argparse
import csv
import json
from pathlib import Path


def export_forecasts(report, path):
    labels = report.get("outcome_labels", {})
    with Path(path).open("x", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "market_id",
                "cluster",
                "predicted_at",
                "label_known_at",
                "probability",
                "outcome",
            ],
        )
        writer.writeheader()
        for forecast in report.get("forecasts", []):
            label = labels.get(forecast["market_id"])
            writer.writerow(
                dict(
                    market_id=forecast["market_id"],
                    cluster=forecast["cluster"],
                    predicted_at=forecast["at"],
                    probability=forecast["p"],
                    label_known_at=label["known_at"] if label else "",
                    outcome=label["outcome"] if label else "",
                )
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.report, encoding="utf-8") as source:
        report = json.load(source)
    if "forecasts" not in report or "outcome_labels" not in report:
        raise ValueError("report predates forecast/outcome export support")
    export_forecasts(report, args.output)


if __name__ == "__main__":
    main()
