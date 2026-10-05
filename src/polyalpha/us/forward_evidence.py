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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..research_dataset import ResearchDataset
from .families import default_split_date, evaluate_all_families
from .research import build_us_research_dataset, sampling_hierarchy

REAL_DATA_START_US = datetime(2026, 9, 16, 2, 49, 12, tzinfo=UTC)
MIN_DAYS = 30
MIN_SETTLED = 200
MIN_CLUSTERS = 200
# Minimum fraction of the nominal forward window that must have observations
# (i.e. not fall inside a known outage) before family evaluation. Added as an
# evidence-quality gate before the boundary was crossed; see docs/RESEARCH_AGENDA.md.
MIN_COVERAGE = 0.8

# Known observation gaps in the US retail lineage (e.g. collector outages).
# Read-only provenance: the report carries them so downstream analysis never
# treats an unobserved interval as observed. Never used to synthesize data.
DEFAULT_KNOWN_GAPS = (
    Path(__file__).resolve().parents[3] / "config" / "us_known_gaps.json"
)


def _load_known_gaps(path: str | Path | None) -> list[dict]:
    if path is None:
        return []
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    gaps = data.get("gaps", []) if isinstance(data, dict) else []
    return gaps if isinstance(gaps, list) else []


def _settlement_completeness(dataset: ResearchDataset) -> dict:
    """Markets whose end date has passed but which still lack a binary
    settlement. A high value is a settlement-poll gap (e.g. after an outage);
    a small residual is expected from non-binary (void/split) resolutions.
    """
    ended: set[str] = set()
    ended_unsettled: set[str] = set()
    no_enddate: set[str] = set()
    for s in dataset.snapshots:
        mid = s.market_id
        h = s.hours_to_resolution
        if h is None:
            no_enddate.add(mid)
        elif h < 0:
            ended.add(mid)
            if s.final_resolution is None:
                ended_unsettled.add(mid)
    return {
        "markets_ended": len(ended),
        "ended_without_binary_settlement": len(ended_unsettled),
        "markets_without_enddate_metadata": len(no_enddate),
    }


def _parse_gap_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _observed_coverage(as_of: datetime, known_gaps: list[dict]) -> float:
    """Fraction of the nominal forward window that actually has observations.

    The nominal window is [REAL_DATA_START_US, as_of]; each known gap's overlap
    with that window counts as unobserved. 1.0 means no recorded outages.
    """
    elapsed = (as_of - REAL_DATA_START_US).total_seconds()
    if elapsed <= 0:
        return 1.0
    gap_seconds = 0.0
    for gap in known_gaps:
        start = _parse_gap_ts(gap.get("start"))
        end = _parse_gap_ts(gap.get("end"))
        if start is None or end is None:
            continue
        lo = max(start, REAL_DATA_START_US)
        hi = min(end, as_of)
        if hi > lo:
            gap_seconds += (hi - lo).total_seconds()
    return max(0.0, 1.0 - gap_seconds / elapsed)


def evaluation_boundary(as_of: datetime | None = None) -> dict:
    now = as_of or datetime.now(UTC)
    days = (now - REAL_DATA_START_US).days
    return {
        "met": days >= MIN_DAYS,
        "days_elapsed": days,
        "days_required": MIN_DAYS,
    }


def forward_evidence_report(
    dataset: ResearchDataset,
    as_of: datetime | None = None,
    known_gaps: list[dict] | None = None,
) -> dict:
    """Produce the forward-evidence report (or PENDING_EVIDENCE status)."""
    now = as_of or datetime.now(UTC)
    boundary = evaluation_boundary(now)
    hierarchy = sampling_hierarchy(dataset)
    settled = hierarchy["resolved_markets"]
    clusters = hierarchy["effective_n"]
    completeness = _settlement_completeness(dataset)
    coverage = _observed_coverage(now, known_gaps or [])

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
        "observed_coverage": {
            "met": coverage >= MIN_COVERAGE,
            "value": round(coverage, 4),
            "required": MIN_COVERAGE,
        },
    }
    met = all(g["met"] for g in gates.values())

    report: dict = {
        "as_of": now.isoformat(),
        "real_data_start_us": REAL_DATA_START_US.isoformat(),
        "split_date": default_split_date().isoformat(),
        "evaluation_boundary_met": met,
        "gates": gates,
        "sampling_hierarchy": hierarchy,
        "settlement_completeness": completeness,
        "data_gaps": known_gaps or [],
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


def build_report_from_raw(
    raw_dir: str | Path,
    as_of: datetime | None = None,
    release_mapping_path: str | Path | None = None,
    progress: Callable[[int, int, int], None] | None = None,
    gaps_config: str | Path | None = None,
) -> dict:
    if gaps_config is None:
        gaps_config = DEFAULT_KNOWN_GAPS
    known_gaps = _load_known_gaps(gaps_config)
    dataset = build_us_research_dataset(
        raw_dir, release_mapping_path=release_mapping_path, progress=progress
    )
    return forward_evidence_report(dataset, as_of, known_gaps=known_gaps)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Forward-evidence evaluation report")
    parser.add_argument("--raw-dir", default="data/us/retail/raw")
    parser.add_argument("--release-mapping", default=None,
                        help="Path to release_mapping.jsonl (macro parent-event grouping)")
    parser.add_argument("--output", default=None, help="Write report JSON here")
    args = parser.parse_args()

    report = build_report_from_raw(args.raw_dir, release_mapping_path=args.release_mapping)
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
