#!/usr/bin/env python3
"""Read-only audit: reproduce the forward-eval settled-markets gate.

Independently rebuilds the US research dataset and runs the FROZEN
``_resolved_eval`` predicate (``src/polyalpha/us/families.py``) to enumerate the
markets counted by the ``settled_markets`` gate, and explains WHY each qualifies.
It is a non-authoritative integrity check: it writes only a derived report,
never research state, never a snapshot, never a family verdict, and it PRINTS
discrepancies rather than "fixing" anything.

Default mode SKIPS the 27 GB events feed (parent cluster falls back to slug /
release mapping). The settled-markets gate value does not depend on the parent
cluster, so default mode answers the question that matters:

    Does every counted market satisfy the frozen eval predicates?

``--with-events`` additionally resolves exact parent event IDs for
provenance/cluster forensics only (it does NOT change the gate).

Usage:
    .venv/Scripts/python.exe scripts/audit_forward_eval_settled.py
    .venv/Scripts/python.exe scripts/audit_forward_eval_settled.py --with-events
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.research_dataset import FeatureProvenance, MarketSnapshot, ResearchDataset  # noqa: E402
from polyalpha.us.families import _end_date, _resolved_eval, default_split_date  # noqa: E402
from polyalpha.us.research import (  # noqa: E402
    SOURCE_BOOK,
    SOURCE_EVENTS,
    SOURCE_MARKETS,
    SOURCE_SETTLEMENT,
    _binary_label,
    _book_bbo,
    _default_release_mapping,
    _latest_meta_at,
    _load_release_parents,
    _parse_iso,
    _ts_from_ns,
)
from polyalpha.us.states import UsMarketState  # noqa: E402

DEFAULT_RAW = ROOT / "data" / "us" / "retail" / "raw"
DEFAULT_OUT = ROOT / "data" / "us" / "reports" / "audit_forward_eval_settled.json"


def _iter_records(raw_dir: str | Path, sources: set[str]):
    """Stream envelope records for ONLY the given source prefixes.

    Unlike ``RawStore.replay`` (which reads every file), this skips files whose
    name does not match a requested source, so the 27 GB events feed is never
    traversed in default mode.
    """
    for dirpath, _dirs, filenames in os.walk(Path(raw_dir)):
        for fn in sorted(filenames):
            source = next((s for s in sources if fn.startswith(s + "-")), None)
            if source is None:
                continue
            path = os.path.join(dirpath, fn)
            try:
                fh = gzip.open(path, "rt", encoding="utf-8") if fn.endswith(".gz") else open(path, "r", encoding="utf-8")
            except OSError:
                continue
            with fh:
                try:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            base = json.loads(data["canonical_json"])
                        except (json.JSONDecodeError, KeyError, ValueError):
                            continue
                        yield base["source"], base["received_at_ns"], base["payload"]
                except EOFError:
                    continue


def build_dataset_mirror(
    raw_dir: str | Path,
    release_mapping_path: str | Path | None = None,
    include_events: bool = False,
) -> tuple[ResearchDataset, dict[str, list[tuple[int, dict]]]]:
    """Faithful mirror of ``build_us_research_dataset`` (us/research.py).

    The ONLY deviation is the ``include_events`` switch: the events feed is read
    solely to fill parent-event clusters, which the settled-markets gate does not
    use. Returns ``(dataset, metadata_timelines)`` where the latter is the raw
    per-slug metadata timeline (for endDate-revision forensics).
    """
    if release_mapping_path is None:
        release_mapping_path = _default_release_mapping(Path(raw_dir))
    markets_by_slug: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    settlement_by_slug: dict[str, tuple[int | None, int]] = {}
    parent_event_by_slug: dict[str, str] = {}

    pass1_sources = {SOURCE_MARKETS, SOURCE_SETTLEMENT}
    if include_events:
        pass1_sources.add(SOURCE_EVENTS)
    for source, received_at_ns, payload in _iter_records(raw_dir, pass1_sources):
        if source == SOURCE_MARKETS:
            for market in payload.get("markets", []):
                slug = market.get("slug")
                if slug:
                    meta = {key: market[key] for key in ("endDate", "category", "question") if key in market}
                    markets_by_slug[slug].append((received_at_ns, meta))
        elif include_events and source == SOURCE_EVENTS:
            for event in payload.get("events", []):
                event_id = event.get("id")
                if event_id is None:
                    continue
                for market in event.get("markets", []):
                    slug = market.get("slug")
                    if slug:
                        parent_event_by_slug[slug] = str(event_id)
        elif source == SOURCE_SETTLEMENT:
            slug = payload.get("slug")
            if slug:
                settlement_by_slug[slug] = (_binary_label(payload.get("settlement")), received_at_ns)

    market_meta_ts: dict[str, list[int]] = {}
    for slug, timeline in markets_by_slug.items():
        timeline.sort(key=lambda t: t[0])
        market_meta_ts[slug] = [ts for ts, _ in timeline]

    for slug, release_id in _load_release_parents(release_mapping_path).items():
        parent_event_by_slug.setdefault(slug, release_id)

    snapshots: list[MarketSnapshot] = []
    for source, received_at_ns, payload in _iter_records(raw_dir, {SOURCE_BOOK}):
        market_data = payload.get("marketData", payload) or {}
        slug = market_data.get("marketSlug")
        if not slug:
            continue

        state_raw = market_data.get("state")
        if state_raw:
            try:
                if not UsMarketState.parse(state_raw).is_tradable():
                    continue
            except ValueError:
                pass

        observed = _ts_from_ns(received_at_ns)
        bbo = _book_bbo(market_data)
        stats = market_data.get("stats") or {}
        timeline = markets_by_slug.get(slug, [])
        meta = _latest_meta_at(timeline, market_meta_ts.get(slug, []), received_at_ns)
        end = _parse_iso(meta.get("endDate")) if meta else None
        hours = (end - observed).total_seconds() / 3600.0 if end is not None else None

        settlement = settlement_by_slug.get(slug)
        final = settlement[0] if settlement else None
        resolution_ts = _ts_from_ns(settlement[1]) if settlement else None

        internal = f"us:{slug}"
        cluster = parent_event_by_slug.get(slug, slug)
        snapshots.append(
            MarketSnapshot(
                observation_timestamp=observed,
                market_id=internal,
                event_id=cluster,
                condition_id=internal,
                category=meta.get("category", "unknown") if meta else "unknown",
                question=meta.get("question", "") if meta else "",
                yes_token_id=f"{internal}:LONG",
                no_token_id=f"{internal}:SHORT",
                yes_best_bid=bbo["yes_best_bid"],
                yes_best_ask=bbo["yes_best_ask"],
                yes_mid=bbo["yes_mid"],
                yes_spread=bbo["yes_spread"],
                yes_bid_size=bbo["yes_bid_size"],
                yes_ask_size=bbo["yes_ask_size"],
                yes_depth_1=bbo["yes_depth_1"],
                yes_depth_5=bbo["yes_depth_5"],
                yes_depth_10=bbo["yes_depth_10"],
                hours_to_resolution=hours,
                days_to_resolution=(hours / 24.0) if hours is not None else None,
                event_cluster=cluster,
                final_resolution=final,
                resolution_timestamp=resolution_ts,
                feature_provenance=FeatureProvenance(
                    source_timestamp=_parse_iso(market_data.get("transactTime")),
                    retrieval_timestamp=observed,
                ),
            )
        )

    resolved = [s for s in snapshots if s.final_resolution is not None]
    categories: dict[str, int] = defaultdict(int)
    for s in snapshots:
        categories[s.category] += 1

    dataset = ResearchDataset(
        snapshots=snapshots,
        created_at=datetime.now(UTC),
        source_reports=["us_raw_store"],
        data_hash="",
        total_observations=len(snapshots),
        resolved_observations=len(resolved),
        unresolved_observations=len(snapshots) - len(resolved),
        unique_markets=len({s.market_id for s in snapshots}),
        unique_events=len({s.event_id for s in snapshots}),
        unique_clusters=len({s.event_cluster for s in snapshots}),
        categories=dict(categories),
    )
    return dataset, dict(markets_by_slug)


# Independent literal translation of the frozen spec (RESEARCH_AGENDA.md):
#   temporal split: endDate >= SPLIT_DATE, where SPLIT_DATE = min(REAL_DATA_START
#   + 45d, 2026-11-01) and REAL_DATA_START = 2026-09-16.
#   binary settlement; usable price (yes_mid present); prediction observed
#   before resolution.
# Deliberately does NOT import _resolved_eval/_end_date/default_split_date so a
# bug in the evaluator predicate cannot silently self-confirm.
INDEPENDENT_SPLIT_DATE = min(date(2026, 9, 16) + timedelta(days=45), date(2026, 11, 1))


def independent_resolved_eval(dataset: ResearchDataset, split_date: date = INDEPENDENT_SPLIT_DATE) -> list[MarketSnapshot]:
    """Separately implemented literal translation of the frozen predicate."""
    out: list[MarketSnapshot] = []
    for s in dataset.snapshots:
        if s.final_resolution not in (0, 1):      # binary settlement
            continue
        if s.yes_mid is None:                     # usable price (bid+ask present)
            continue
        if s.hours_to_resolution is None:         # no endDate -> not evaluable
            continue
        end = s.observation_timestamp + timedelta(hours=s.hours_to_resolution)
        if end.date() < split_date:               # endDate >= SPLIT_DATE
            continue
        if s.resolution_timestamp is not None and s.observation_timestamp >= s.resolution_timestamp:
            continue                              # prediction observed before resolution
        out.append(s)
    return out


def run_audit(raw_dir: str | Path, release_mapping_path: str | Path | None, include_events: bool) -> dict:
    split = default_split_date()
    dataset, timelines = build_dataset_mirror(raw_dir, release_mapping_path, include_events)

    # FROZEN predicate: this is the exact call the evaluator makes.
    settled_snaps = _resolved_eval(dataset, split)

    # Group by market_id.
    by_market: dict[str, list[MarketSnapshot]] = defaultdict(list)
    for s in settled_snaps:
        by_market[s.market_id].append(s)

    release_parents = _load_release_parents(release_mapping_path)
    markets: list[dict] = []
    split_crossing = 0
    enddate_revision = 0
    for mid in sorted(by_market):
        snaps = by_market[mid]
        first = sorted(snaps, key=lambda s: s.observation_timestamp)[0]
        slug = mid[3:] if mid.startswith("us:") else mid
        timeline = sorted(timelines.get(slug, []), key=lambda t: t[0])

        # Reconstructed endDate across ALL book snapshots of this market (not just
        # eligible ones), to detect a metadata revision that moves it across the
        # split. The frozen reconstruction is observation + hours_to_resolution.
        end_dates: set[str] = set()
        for s in dataset.snapshots:
            if s.market_id != mid:
                continue
            e = _end_date(s)
            if e is not None:
                end_dates.add(e.date().isoformat())

        # Raw metadata endDate values (revision history) straight from the
        # metadata timeline — this is what demonstrates 16:20Z -> 21:20Z.
        end_raw: dict[str, int] = defaultdict(int)
        for _recv_ns, meta in timeline:
            raw_end = meta.get("endDate")
            if raw_end:
                end_raw[str(raw_end)] += 1

        dates = sorted(end_dates)
        crosses = any(d < split.isoformat() for d in dates) and any(d >= split.isoformat() for d in dates)
        if crosses:
            split_crossing += 1
        if len(end_raw) > 1:
            enddate_revision += 1

        # Latest metadata for category/question context.
        latest_meta = timeline[-1][1] if timeline else {}
        markets.append({
            "market_id": mid,
            "slug": slug,
            "parent_cluster": first.event_cluster,
            "cluster_source": "events" if include_events else ("release_mapping" if slug in release_parents else "slug_fallback"),
            "category": latest_meta.get("category", "unknown"),
            "question": latest_meta.get("question", ""),
            "final_resolution": first.final_resolution,
            "resolution_timestamp": first.resolution_timestamp.isoformat() if first.resolution_timestamp else None,
            "first_eligible_snapshot_ts": min(s.observation_timestamp for s in snaps).isoformat(),
            "n_eligible_snapshots": len(snaps),
            "reconstructed_endDate_dates": sorted(end_dates),
            "raw_endDate_values": dict(sorted(end_raw.items())),
            "split_crossing": crosses,
            "why_qualifies": {
                "binary_settlement": first.final_resolution is not None,
                "has_yes_mid": first.yes_mid is not None,
                "endDate_ge_split": (_end_date(first) is not None and _end_date(first).date() >= split),
                "prediction_before_resolution": (
                    first.resolution_timestamp is None or first.observation_timestamp < first.resolution_timestamp
                ),
            },
        })

    gate_value = len({s.market_id for s in settled_snaps})
    all_qualify = all(all(m["why_qualifies"].values()) for m in markets)

    # Independent literal translation vs the canonical predicate — so a bug in
    # the evaluator predicate cannot silently self-confirm.
    independent_snaps = independent_resolved_eval(dataset)
    canonical_ids = {s.market_id for s in settled_snaps}
    independent_ids = {s.market_id for s in independent_snaps}
    canonical_only = sorted(canonical_ids - independent_ids)
    independent_only = sorted(independent_ids - canonical_ids)
    symmetric_difference = sorted(canonical_ids ^ independent_ids)
    predicates_agree = (canonical_ids == independent_ids)
    split_dates_agree = (split == INDEPENDENT_SPLIT_DATE)

    verdict = (
        "SETTLED-GATE IMPLEMENTATION VERIFIED"
        if (all_qualify and split_crossing == 0 and predicates_agree and split_dates_agree)
        else "DISCREPANCY_FOUND"
    )
    discrepancies: list[dict] = []
    if not all_qualify:
        discrepancies.append({"type": "market_fails_predicate",
                              "markets": [m["market_id"] for m in markets if not all(m["why_qualifies"].values())]})
    if split_crossing:
        discrepancies.append({"type": "split_crossing",
                              "markets": [m["market_id"] for m in markets if m["split_crossing"]]})
    if not split_dates_agree:
        discrepancies.append({"type": "split_date_mismatch",
                              "canonical": split.isoformat(),
                              "independent": INDEPENDENT_SPLIT_DATE.isoformat()})
    if not predicates_agree:
        discrepancies.append({"type": "predicate_mismatch",
                              "canonical_only": canonical_only,
                              "independent_only": independent_only,
                              "symmetric_difference": symmetric_difference})

    return {
        "audit": "forward_eval_settled_markets",
        "as_of": datetime.now(UTC).isoformat(),
        "split_date": split.isoformat(),
        "independent_split_date": INDEPENDENT_SPLIT_DATE.isoformat(),
        "frozen_predicate": "_resolved_eval + _end_date + default_split_date (src/polyalpha/us/families.py)",
        "independent_predicate": "literal translation (binary settlement; yes_mid present; "
                                 "endDate>=split via observation + hours_to_resolution; prediction before resolution)",
        "include_events": include_events,
        "gate_value": gate_value,
        "predicate_comparison": {
            "canonical_count": len(canonical_ids),
            "independent_count": len(independent_ids),
            "predicates_agree": predicates_agree,
            "canonical_only": canonical_only,
            "independent_only": independent_only,
            "symmetric_difference": symmetric_difference,
        },
        "summary": {
            "counted_markets": gate_value,
            "markets_satisfying_all_predicates": sum(1 for m in markets if all(m["why_qualifies"].values())),
            "split_crossing_markets": split_crossing,
            "enddate_revision_markets": enddate_revision,
            "split_dates_agree": split_dates_agree,
            "predicates_agree": predicates_agree,
            "verdict": verdict,
        },
        "discrepancies": discrepancies,
        "markets": markets,
    }


def _human(report: dict) -> str:
    s = report["summary"]
    lines = [
        "=" * 72,
        "FORWARD-EVAL SETTLED-MARKETS GATE AUDIT (read-only)",
        "=" * 72,
        f"split_date={report['split_date']}  include_events={report['include_events']}",
        f"frozen predicate: {report['frozen_predicate']}",
        "",
        f"  counted markets (gate value)          : {s['counted_markets']}",
        f"  markets satisfying all predicates     : {s['markets_satisfying_all_predicates']}",
        f"  split-crossing markets               : {s['split_crossing_markets']}",
        f"  markets with endDate revision         : {s['enddate_revision_markets']}",
        "",
        f"  split dates agree (canonical vs independent) : {s['split_dates_agree']}",
        f"  predicates agree (canonical vs independent)  : {s['predicates_agree']}",
        f"  canonical_only        : {report['predicate_comparison']['canonical_only']}",
        f"  independent_only      : {report['predicate_comparison']['independent_only']}",
        f"  symmetric_difference  : {report['predicate_comparison']['symmetric_difference']}",
        "",
        f"  VERDICT: {s['verdict']}",
    ]
    if report["discrepancies"]:
        lines.append("")
        lines.append("DISCREPANCIES (printed, not fixed):")
        for d in report["discrepancies"]:
            lines.append(f"  - {d['type']}: {d}")
    lines.append("")
    lines.append("ENDDATE REVISION NOTE (preserved for future audits):")
    lines.append("  raw endDate moved 16:20Z -> 21:20Z on listing day, but")
    lines.append("  date(endDate)=2026-11-06 throughout => NO split-crossing,")
    lines.append("  no methodological defect.")
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only forward-eval settled-markets gate audit")
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW))
    ap.add_argument("--release-mapping", default=None,
                    help="path to release_mapping.jsonl (macro parent grouping)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="report JSON path")
    ap.add_argument("--with-events", action="store_true",
                    help="read the 27 GB events feed for exact parent event IDs (forensics only)")
    args = ap.parse_args()

    report = run_audit(args.raw_dir, args.release_mapping, args.with_events)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    print(_human(report))
    # Compact per-market table.
    print(f"\n{'slug':52s} {'res':>3s} {'endDate':>12s} {'splitCross':>10s}")
    for m in report["markets"]:
        ed = m["reconstructed_endDate_dates"]
        ed_disp = ",".join(ed) if len(ed) <= 2 else f"{len(ed)}dates"
        print(f"{m['slug']:52s} {str(m['final_resolution']):>3s} {ed_disp:>12s} {str(m['split_crossing']):>10s}")
    print(f"\nWROTE {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())