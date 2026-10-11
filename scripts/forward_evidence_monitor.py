#!/usr/bin/env python3
"""Forward-evidence progress monitor (read-only, fast).

One-glance view of progress toward the pre-registered evaluation boundary
(>= 30 days, >= 200 settled markets, >= 200 independent clusters, and >= 0.8
observed coverage) plus the upcoming macro resolutions that will drive the
settled-markets gate.

Does NOT replay the raw store (that is ~2 minutes). It reads the persisted
membership files (tracked/settled), the release schedule + mapping, and the
latest daily snapshot. Pass --refresh to rebuild the snapshot first so the
cluster/settled numbers are current.

Usage:
    python scripts/forward_evidence_monitor.py
    python scripts/forward_evidence_monitor.py --refresh
    python scripts/forward_evidence_monitor.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "us"
REAL_DATA_START_US = datetime(2026, 9, 16, 2, 49, 12, tzinfo=UTC)
MIN_DAYS = 30
MIN_SETTLED = 200
MIN_CLUSTERS = 200
MIN_COVERAGE = 0.8


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _membership_count(path: Path) -> int:
    slugs = {r["market_slug"] for r in _load_jsonl(path) if r.get("market_slug")}
    return len(slugs)


def _latest_snapshot() -> tuple[dict | None, str | None]:
    reports = sorted((DATA / "reports").glob("forward-evidence-*.json"))
    if not reports:
        return None, None
    return json.loads(reports[-1].read_text(encoding="utf-8")), reports[-1].name


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _release_calendar(now: datetime) -> list[dict]:
    schedule_path = ROOT / "config" / "gov_releases.json"
    if not schedule_path.exists():
        return []
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    slugs_by_release: dict[str, set[str]] = {}
    for rec in _load_jsonl(DATA / "release_mapping.jsonl"):
        rid, slug = rec.get("release_id"), rec.get("market_slug")
        if rid and slug:
            slugs_by_release.setdefault(rid, set()).add(slug)
    out = []
    for rel in schedule.get("releases", []):
        rid = rel.get("event_id")
        at = datetime.fromisoformat(rel["scheduled_at"].replace("Z", "+00:00"))
        out.append({
            "release_id": rid,
            "name": rel.get("name", ""),
            "at": rel["scheduled_at"],
            "markets": len(slugs_by_release.get(rid, set())),
            "resolved": at < now,
        })
    return out


def _build_summary(now: datetime) -> dict:
    snap, snap_name = _latest_snapshot()
    tracked = _membership_count(DATA / "tracked_membership.jsonl")
    settled = _membership_count(DATA / "settled_membership.jsonl")
    days = (now - REAL_DATA_START_US).days

    gates = {"days": days, "clusters": None, "settled": None, "coverage": None, "as_of": None}
    coverage_required = MIN_COVERAGE
    status = None
    families_evaluated = 0
    gap_count = 0
    gap_walltime_seconds = 0.0
    if snap:
        g = snap.get("gates", {})
        gates["clusters"] = g.get("independent_clusters", {}).get("value")
        gates["settled"] = g.get("settled_markets", {}).get("value")
        gates["coverage"] = g.get("observed_coverage", {}).get("value")
        cov_required = g.get("observed_coverage", {}).get("required")
        if cov_required is not None:
            coverage_required = float(cov_required)
        gates["as_of"] = snap.get("as_of")
        status = snap.get("status")
        families_evaluated = len(snap.get("families", []) or [])
        gaps = snap.get("data_gaps", []) or []
        gap_count = len(gaps)
        for gap in gaps:
            lo = _parse_iso(gap.get("start"))
            hi = _parse_iso(gap.get("end"))
            if lo is not None and hi is not None and hi > lo:
                gap_walltime_seconds += (hi - lo).total_seconds()

    upcoming = [r for r in _release_calendar(now) if not r["resolved"]]
    return {
        "now": now.isoformat(),
        "days": days,
        "tracked_universe": tracked,
        "settled_membership": settled,
        "gates": gates,
        "snapshot": snap_name,
        "upcoming_releases": upcoming,
        "required": {
            "days": MIN_DAYS,
            "settled": MIN_SETTLED,
            "clusters": MIN_CLUSTERS,
            "coverage": coverage_required,
        },
        "status": status,
        "families_evaluated": families_evaluated,
        "gap_count": gap_count,
        "gap_walltime_seconds": gap_walltime_seconds,
    }


def _render(s: dict) -> str:
    req = s["required"]
    g = s["gates"]

    def _v(x: object) -> str:
        return "n/a" if x is None else str(x)

    def _mark(met: bool) -> str:
        return "MET" if met else "PENDING"

    def _gate(label: str, value: str, required: str, met: bool, binding: bool = False) -> str:
        state = _mark(met) + ("  <-- binding" if binding else "")
        return f"  {label:<22}{value:>8} / {required:<6} {state}"

    days_met = s["days"] >= req["days"]
    settled_met = g["settled"] is not None and g["settled"] >= req["settled"]
    clusters_met = g["clusters"] is not None and g["clusters"] >= req["clusters"]
    coverage_met = g["coverage"] is not None and g["coverage"] >= req["coverage"]

    cov_val = f"{g['coverage'] * 100:.2f}%" if g["coverage"] is not None else "n/a"
    cov_req = f"{req['coverage'] * 100:.0f}%"
    alpha = "UNKNOWN" if (s["status"] or "") == "PENDING_EVIDENCE" else "EVALUATED"

    lines = [
        "Forward evidence",
        "----------------",
        _gate("Days:", str(s["days"]), str(req["days"]), days_met),
        _gate("Settled markets:", _v(g["settled"]), str(req["settled"]), settled_met),
        _gate("Independent clusters:", _v(g["clusters"]), str(req["clusters"]), clusters_met),
        _gate("Observed coverage:", cov_val, cov_req, coverage_met, binding=not coverage_met),
        "",
        f"  Status: {s['status'] or 'NO_SNAPSHOT'}",
        f"  Alpha:  {alpha}",
        f"  Families evaluated: {s['families_evaluated']}",
        f"  Known gaps: {s['gap_count']} interval(s), "
        f"~{s['gap_walltime_seconds'] / 86400.0:.1f} days unobserved",
        "",
        f"  now:                            {s['now']}",
        f"  tracked universe (additive):    {s['tracked_universe']}   <- leading indicator for clusters",
        f"  settled membership:             {s['settled_membership']}",
        "",
    ]
    if s["snapshot"]:
        lines.append(f"  snapshot: {s['snapshot']}  (run --refresh to rebuild)")
    lines.append("")
    lines.append("  upcoming macro resolutions (drive settled-markets gate):")
    if s["upcoming_releases"]:
        for r in s["upcoming_releases"]:
            lines.append(
                f"    {r['at']}  {r['release_id']:<18} {r['markets']:>3} markets   {r['name']}"
            )
    else:
        lines.append("    (none)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward-evidence progress monitor")
    parser.add_argument("--refresh", action="store_true",
                        help="Rebuild the daily snapshot first (slow, replays raw store)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    if args.refresh:
        import subprocess

        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "daily_forward_evidence.py")],
            cwd=ROOT,
            check=False,
        )

    summary = _build_summary(datetime.now(UTC))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print(_render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
