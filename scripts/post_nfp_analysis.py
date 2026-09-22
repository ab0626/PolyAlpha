#!/usr/bin/env python3
"""Preregistered NFP #1 analysis (read-only, consumes only frozen outputs).

Produces exactly the predefined views in docs/NFP_ANALYSIS_TEMPLATE.md. No new
formulas, thresholds, or views. Unsupported quantities render INSUFFICIENT_DATA.
Conclusion is INSTRUMENT_VALIDATION_* — never "strategy passed".
"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.event_shock import EventShock, measure_shock_reaction  # noqa: E402
from polyalpha.gov_releases import load_gov_releases  # noqa: E402
from polyalpha.integration import raw_provenance, shock_id  # noqa: E402
from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.us.adapter import amount_to_decimal  # noqa: E402
from polyalpha.us.fills import parse_us_trade  # noqa: E402
from polyalpha.us.quote_race import Quote  # noqa: E402


def _ts(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, UTC)


def _load_quotes(raw_dir: str) -> dict[str, list[Quote]]:
    out: dict[str, list[Quote]] = {}
    for r in RawStore(raw_dir).replay():
        if r.source != "polymarket_us_retail_book":
            continue
        md = r.payload.get("marketData", r.payload) or {}
        slug = md.get("marketSlug")
        if not slug:
            continue
        bids, offers = md.get("bids") or [], md.get("offers") or []
        if not bids or not offers:
            continue
        out.setdefault(slug, []).append(
            Quote(slug, _ts(r.received_at_ns),
                  amount_to_decimal(bids[0]["px"]), amount_to_decimal(offers[0]["px"]))
        )
    for qs in out.values():
        qs.sort(key=lambda q: q.timestamp)
    return out


def _load_fills(raw_dir: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in RawStore(raw_dir).replay():
        if r.source != "polymarket_us_retail_trade":
            continue
        try:
            f = parse_us_trade(r.payload, _ts(r.received_at_ns))
        except (ValueError, KeyError):
            continue
        if f.market_slug:
            out.setdefault(f.market_slug, []).append(f)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Preregistered NFP #1 analysis")
    parser.add_argument("--manifest", default=str(ROOT / "config" / "analysis_template.json"))
    parser.add_argument("--schedule", default=str(ROOT / "config" / "gov_releases.json"))
    parser.add_argument("--book-raw", default="data/us/retail/raw")
    parser.add_argument("--trade-raw", default="data/us/trade/raw")
    parser.add_argument("--coverage-report", default="data/us/reports/release-coverage.json")
    parser.add_argument("--event-id", default="nfp-2026-10-02")
    parser.add_argument("--pre-seconds", type=int, default=600)
    parser.add_argument("--stable-seconds", type=int, default=1800)
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))

    # Self-hash: prove the inspection template has not drifted.
    own_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    drifted = own_sha != manifest.get("analysis_template_sha256")

    source = load_gov_releases(args.schedule)
    rel = source.by_id(args.event_id)
    if rel is None:
        print(json.dumps({"status": "UNKNOWN_EVENT"}))
        return 1

    sid = shock_id(rel.event_id, rel.scheduled_at)
    shock = EventShock(
        shock_id=sid, event_id=rel.event_id, category=rel.category,
        source="official", source_authority=1.0, primary_source=True,
        first_public_at=rel.scheduled_at, first_received_at=rel.scheduled_at,
        first_processed_at=rel.scheduled_at,
    )

    coverage = {}
    if Path(args.coverage_report).exists():
        coverage = json.loads(Path(args.coverage_report).read_text(encoding="utf-8"))

    quotes_by_slug = _load_quotes(args.book_raw)
    fills_by_slug = _load_fills(args.trade_raw)

    slugs = sorted({m["market_slug"] for m in coverage.get("per_market", [])
                    if m.get("release_id") == args.event_id})
    start = rel.scheduled_at.replace(tzinfo=rel.scheduled_at.tzinfo)
    window_start = start - timedelta(seconds=args.pre_seconds)
    window_end = start + timedelta(seconds=args.stable_seconds)

    reactions = {}
    for slug in slugs:
        quotes = [q for q in quotes_by_slug.get(slug, []) if window_start <= q.timestamp <= window_end]
        fills = [f for f in fills_by_slug.get(slug, []) if window_start <= f.trade_time <= window_end]
        if not quotes:
            reactions[slug] = {"status": "INSUFFICIENT_DATA"}
            continue
        reactions[slug] = measure_shock_reaction(
            shock, f"us:{slug}", quotes, fills,
            stable_seconds=args.stable_seconds, feed_label="REST_2S",
        ).summary()

    provenance = raw_provenance(args.book_raw, window_start, window_end, tuple(f"us:{s}" for s in slugs))

    report = {
        "manifest": manifest,
        "analysis_template_sha256": own_sha,
        "template_drifted": drifted,
        "event_id": args.event_id,
        "scheduled_at": rel.scheduled_at.isoformat(),
        "instrument_validation": {
            "coverage_ok": coverage.get("next_release_coverage_ok"),
            "required_markets_missing": coverage.get("required_markets_missing"),
        },
        "reactions": reactions,
        "raw_provenance": provenance,
        "propagation": {"Z_lag": "INSUFFICIENT_DATA"},
        "conclusion": "INSTRUMENT_VALIDATION_ONLY",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
