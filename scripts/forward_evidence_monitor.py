#!/usr/bin/env python3
"""Forward-evidence progress monitor (read-only, fast).

One-glance view of progress toward the pre-registered evaluation boundary
(>= 30 days, >= 200 settled markets, >= 200 independent clusters) plus the
upcoming macro resolutions that will drive the settled-markets gate.

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

    gates = {"days": days, "clusters": None, "settled": None, "as_of": None}
    if snap:
        g = snap.get("gates", {})
        gates["clusters"] = g.get("independent_clusters", {}).get("value")
        gates["settled"] = g.get("settled_markets", {}).get("value")
        gates["as_of"] = snap.get("as_of")

    upcoming = [r for r in _release_calendar(now) if not r["resolved"]]
    return {
        "now": now.isoformat(),
        "days": days,
        "tracked_universe": tracked,
        "settled_membership": settled,
        "gates": gates,
        "snapshot": snap_name,
        "upcoming_releases": upcoming,
        "required": {"days": MIN_DAYS, "settled": MIN_SETTLED, "clusters": MIN_CLUSTERS},
    }


def _render(s: dict) -> str:
    req = s["required"]
    lines = [
        "FORWARD-EVIDENCE PROGRESS",
        f"  now:               {s['now']}",
        "",
        "  gates (pre-registered):",
        f"    days elapsed       {s['days']:>4} / {req['days']:>4}"
        + ("  <-- MET" if s["days"] >= req["days"] else ""),
        f"    settled markets    {str(s['gates']['settled']):>4} / {req['settled']:>4}"
        + (f"  (as of {s['gates']['as_of']})" if s["gates"]["as_of"] else ""),
        f"    clusters           {str(s['gates']['clusters']):>4} / {req['clusters']:>4}"
        + (f"  (as of {s['gates']['as_of']})" if s["gates"]["as_of"] else ""),
        "",
        f"  tracked universe (additive):  {s['tracked_universe']}   <- leading indicator for clusters",
        f"  settled membership:           {s['settled_membership']}",
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
