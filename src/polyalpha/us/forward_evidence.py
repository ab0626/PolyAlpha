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

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..research_dataset import ResearchDataset
from .families import _resolved_eval, default_split_date, evaluate_all_families
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


def _load_known_gaps(path: str | Path | None) -> list[dict] | None:
    """Load known-gap provenance; ``None`` means missing/unreadable/malformed.

    Fail-closed at TWO levels: a missing/corrupt ledger returns ``None``, AND a
    single malformed row (non-dict, missing/unparseable/naive timestamp, or
    ``end <= start``) rejects the ENTIRE ledger. A malformed row must produce
    coverage=UNKNOWN / gate=FAIL, never silently disappear from the numerator.
    """
    if path is None:
        return None
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    gaps = data.get("gaps") if isinstance(data, dict) else None
    if not isinstance(gaps, list):
        return None
    for gap in gaps:
        if not isinstance(gap, dict):
            return None
        start = _parse_gap_ts(gap.get("start"))
        end = _parse_gap_ts(gap.get("end"))
        if start is None or end is None or end <= start:
            return None
    return gaps


def _settlement_completeness(dataset: ResearchDataset) -> dict:
    """Markets whose end date has passed but which still lack a binary
    settlement. A high value is a settlement-poll gap (e.g. after an outage);
    a small residual is expected from non-binary (void/split) resolutions.

    The snapshot population only contains pre-resolution/tradable books, so an
    ended market is never observed as ``hours_to_resolution < 0``. When no ended
    market appears, the metric is explicitly marked non-assessable rather than
    silently reading as "100% complete".
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
    out: dict = {
        "markets_ended": len(ended),
        "ended_without_binary_settlement": len(ended_unsettled),
        "markets_without_enddate_metadata": len(no_enddate),
    }
    if len(ended) == 0:
        out["assessable"] = False
        out["reason"] = (
            "book snapshots contain only pre-resolution/tradable markets, so no "
            "snapshot is observed past its end date; settlement completeness is "
            "not assessable from this population"
        )
    else:
        out["assessable"] = True
    return out


def _parse_gap_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # naive timestamp is malformed
    return dt


def _union_unobserved_seconds(as_of: datetime, known_gaps: list[dict]) -> float:
    """Union of unobserved seconds from gap overlaps with [REAL_DATA_START_US, as_of].

    Overlapping intervals are merged, never double-counted. Gaps before the start
    clip to REAL_DATA_START_US; gaps after ``as_of`` clip to ``as_of`` (future gaps
    never reduce today's coverage).
    """
    intervals: list[tuple[float, float]] = []
    for gap in known_gaps:
        start = _parse_gap_ts(gap.get("start"))
        end = _parse_gap_ts(gap.get("end"))
        if start is None or end is None:
            continue
        lo = max(start, REAL_DATA_START_US).timestamp()
        hi = min(end, as_of).timestamp()
        if hi > lo:
            intervals.append((lo, hi))
    if not intervals:
        return 0.0
    intervals.sort()
    merged: list[tuple[float, float]] = []
    for lo, hi in intervals:
        if not merged or lo > merged[-1][1]:
            merged.append((lo, hi))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
    return sum(hi - lo for lo, hi in merged)


def _observed_coverage(as_of: datetime, known_gaps: list[dict]) -> float:
    """Fraction of the nominal forward window that actually has observations.

    The nominal window is [REAL_DATA_START_US, as_of]; the UNION of each known
    gap's overlap with that window counts as unobserved. 1.0 means no recorded
    outages.
    """
    elapsed = (as_of - REAL_DATA_START_US).total_seconds()
    if elapsed <= 0:
        return 1.0
    gap_seconds = _union_unobserved_seconds(as_of, known_gaps)
    return max(0.0, 1.0 - gap_seconds / elapsed)


def _ledger_sha256(path: str | Path) -> str | None:
    """SHA-256 of the canonical gap-ledger bytes (LF-normalized)."""
    try:
        return hashlib.sha256(
            Path(path).read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
    except OSError:
        return None


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
    # v2.3: the settled-markets gate counts unique RESOLVED EVAL-SET markets
    # (endDate >= SPLIT_DATE), not all historically resolved markets. This
    # restores the pre-registered rule (">= 200 settled markets in the eval set").
    settled = len({s.market_id for s in _resolved_eval(dataset, default_split_date())})
    clusters = hierarchy["effective_n"]
    completeness = _settlement_completeness(dataset)
    # Fail-closed: missing/corrupt gap provenance must never read as full coverage.
    coverage_unknown = known_gaps is None
    coverage = None if coverage_unknown else _observed_coverage(now, known_gaps)
    unobserved_seconds = None if coverage_unknown else _union_unobserved_seconds(now, known_gaps)
    ledger_sha = _ledger_sha256(DEFAULT_KNOWN_GAPS)
    coverage_gate: dict = {
        "met": (not coverage_unknown) and coverage >= MIN_COVERAGE,
        "value": None if coverage_unknown else round(coverage, 4),
        "required": MIN_COVERAGE,
    }
    if coverage_unknown:
        coverage_gate["error"] = "gap provenance missing/corrupt (fail-closed)"

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
            # Collection-diversity gate: counts ALL distinct parent-event
            # clusters (the independent-ish units), not just resolved/evaluable
            # ones. Each family verdict separately requires >=200 usable/resolved
            # clusters over its own eval sample.
            "note": (
                "collection-diversity gate over all parent-event clusters; "
                "family verdicts separately require >=200 resolved clusters"
            ),
        },
        "observed_coverage": coverage_gate,
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
        "gap_count": len(known_gaps) if known_gaps is not None else None,
        "ledger_sha256": ledger_sha,
        "unobserved_seconds": unobserved_seconds,
        "unobserved_days": (unobserved_seconds / 86400.0) if unobserved_seconds is not None else None,
        "coverage_as_of": now.isoformat(),
        "coverage_provenance": "missing" if coverage_unknown else "ok",
    }
    if coverage_unknown:
        report["errors"] = [
            "gap provenance missing/corrupt: observed coverage is UNKNOWN (fail-closed)"
        ]

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
