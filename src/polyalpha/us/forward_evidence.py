"""Frozen-baseline forward-evidence runner.

Gates on the pre-registered evaluation boundary (docs/RESEARCH_AGENDA.md):
  - >= 30 days since REAL_DATA_START_US
  - >= 200 unique settled markets in the eval set
  - >= 200 independent-ish event clusters

When the boundary is met, it runs the family evaluations and emits the
per-family attribution report. Before that, it reports PENDING_EVIDENCE and
runs nothing (MODEL PERFORMANCE: LOCKED, ALPHA: UNKNOWN).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ..research_dataset import ResearchDataset
from .families import default_split_date, evaluate_all_families
from .research import build_us_research_dataset, sampling_hierarchy

REAL_DATA_START_US = datetime(2026, 9, 16, 2, 49, 12, tzinfo=UTC)
MIN_DAYS = 30
MIN_SETTLED = 200
MIN_CLUSTERS = 200


def evaluation_boundary(as_of: datetime | None = None) -> dict:
    now = as_of or datetime.now(UTC)
    days = (now - REAL_DATA_START_US).days
    return {
        "met": days >= MIN_DAYS,
        "days_elapsed": days,
        "days_required": MIN_DAYS,
    }


def forward_evidence_report(
    dataset: ResearchDataset, as_of: datetime | None = None
) -> dict:
    """Produce the forward-evidence report (or PENDING_EVIDENCE status)."""
    boundary = evaluation_boundary(as_of)
    hierarchy = sampling_hierarchy(dataset)
    settled = hierarchy["resolved_markets"]
    clusters = hierarchy["effective_n"]

    gates = {
        "days": {
            "met": boundary["met"],
            "value": boundary["days_elapsed"],
            "required": MIN_DAYS,
        },
        "settled_markets": {
            "met": settled >= MIN_SETTLED,
            "value": settled,
            "required": MIN_SETTLED,
        },
        "independent_clusters": {
            "met": clusters >= MIN_CLUSTERS,
            "value": clusters,
            "required": MIN_CLUSTERS,
        },
    }
    met = all(g["met"] for g in gates.values())

    report: dict = {
        "as_of": (as_of or datetime.now(UTC)).isoformat(),
        "real_data_start_us": REAL_DATA_START_US.isoformat(),
        "split_date": default_split_date().isoformat(),
        "evaluation_boundary_met": met,
        "gates": gates,
        "sampling_hierarchy": hierarchy,
    }

    if not met:
        report["status"] = "PENDING_EVIDENCE"
        report["families"] = []
        report["message"] = (
            "Evaluation boundary not met: model performance LOCKED, alpha UNKNOWN. "
            "No family evaluation was run."
        )
    else:
        report["status"] = "EVALUATED"
        report["families"] = [d.summary() for d in evaluate_all_families(dataset)]
        report["message"] = (
            "Evaluation boundary met: forward-evidence families evaluated "
            "(per-family attribution, never a single aggregate headline)."
        )
    return report


def build_report_from_raw(raw_dir: str | Path, as_of: datetime | None = None) -> dict:
    dataset = build_us_research_dataset(raw_dir)
    return forward_evidence_report(dataset, as_of)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Forward-evidence evaluation report")
    parser.add_argument("--raw-dir", default="data/us/retail/raw")
    parser.add_argument("--output", default=None, help="Write report JSON here")
    args = parser.parse_args()

    report = build_report_from_raw(args.raw_dir)
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
