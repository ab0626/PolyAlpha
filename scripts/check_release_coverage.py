#!/usr/bin/env python3
"""Pre-release coverage checker: verify every mapped contract is being collected.

Fails loudly (exit 1) when the next release's contracts are not fully covered by
REST + L2 WS + trade WS. Persists the point-in-time mapping and a coverage report
so post-release provenance includes exactly what was believed to matter.

Usage:
    python scripts/check_release_coverage.py
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.us.release_coverage import (  # noqa: E402
    compute_coverage,
    persist_mapping,
    release_required_universe,
)
from polyalpha.us.rest import PublicUsClient  # noqa: E402


def _ts(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, UTC)


def _slug_of(source: str, payload: dict) -> str | None:
    if source in ("polymarket_us_retail_book", "polymarket_us_retail_market_data"):
        md = payload.get("marketData", payload) or {}
        return md.get("marketSlug")
    if source == "polymarket_us_retail_trade":
        tr = payload.get("trade", payload) if isinstance(payload.get("trade"), dict) else payload
        return tr.get("marketSlug")
    return None


def _slugs_and_span(raw_dir: str, source: str) -> tuple[set[str], dict[str, datetime], dict[str, datetime]]:
    slugs: set[str] = set()
    first: dict[str, datetime] = {}
    last: dict[str, datetime] = {}
    for r in RawStore(raw_dir).replay():
        if r.source != source:
            continue
        slug = _slug_of(source, r.payload)
        if not slug:
            continue
        slugs.add(slug)
        ts = _ts(r.received_at_ns)
        if slug not in first or ts < first[slug]:
            first[slug] = ts
        if slug not in last or ts > last[slug]:
            last[slug] = ts
    return slugs, first, last


def main() -> int:
    parser = argparse.ArgumentParser(description="Check release-required coverage")
    parser.add_argument("--schedule", default=str(ROOT / "config" / "gov_releases.json"))
    parser.add_argument("--book-raw", default="data/us/retail/raw")
    parser.add_argument("--l2-raw", default="data/us/market_data/raw")
    parser.add_argument("--trade-raw", default="data/us/trade/raw")
    parser.add_argument("--trade-universe", default="data/us/logs/trade-universe.json")
    parser.add_argument("--mapping", default="data/us/release_mapping.jsonl")
    parser.add_argument("--report", default="data/us/reports/release-coverage.json")
    args = parser.parse_args()

    client = PublicUsClient(timeout=20, attempts=2)
    required = release_required_universe(
        args.schedule,
        lambda term: client.search({"query": term, "status": "active", "limit": 20})[0].get("events", []),
    )
    if not required:
        print(json.dumps({"status": "NO_UPCOMING_RELEASES"}))
        return 0

    rest_slugs, rest_first, rest_last = _slugs_and_span(args.book_raw, "polymarket_us_retail_book")
    l2_slugs, l2_first, l2_last = _slugs_and_span(args.l2_raw, "polymarket_us_retail_market_data")

    # Trade coverage is the *subscription* invariant, not sparse observation:
    # use the persisted trade universe, falling back to observed trade slugs.
    trade_slugs, trade_first, trade_last = _slugs_and_span(args.trade_raw, "polymarket_us_retail_trade")
    trade_universe_path = Path(args.trade_universe)
    if trade_universe_path.exists():
        data = json.loads(trade_universe_path.read_text(encoding="utf-8"))
        trade_slugs = set(data.get("slugs", []) or list(trade_slugs))

    # Collector-level liveness (not per-market activity):
    # REST = age of the latest book record across all markets.
    rest_latest = max(rest_last.values()) if rest_last else None
    rest_collector_age = (datetime.now(UTC) - rest_latest).total_seconds() if rest_latest else None

    # L2 = market-data status fresh + zero required markets missing.
    l2_collector_alive = None
    status_path = Path("data/us/logs/market-data-status.json")
    if status_path.exists():
        st = json.loads(status_path.read_text(encoding="utf-8"))
        st_age = (datetime.now(UTC) - datetime.fromtimestamp(status_path.stat().st_mtime, UTC)).total_seconds()
        l2_collector_alive = st_age < 60 and st.get("required_markets_missing", 0) == 0

    # Trade collector liveness: the universe file is rewritten at each run's
    # startup, so its mtime proxies "is the trade collector still alive".
    trade_collector_age = None
    if trade_universe_path.exists():
        mtime = datetime.fromtimestamp(trade_universe_path.stat().st_mtime, UTC)
        trade_collector_age = (datetime.now(UTC) - mtime).total_seconds()

    report = compute_coverage(
        required, rest_slugs, l2_slugs, trade_slugs,
        rest_last=rest_last, l2_last=l2_last,
        rest_collector_age_seconds=rest_collector_age,
        l2_collector_alive=l2_collector_alive,
        trade_collector_age_seconds=trade_collector_age,
    )
    report["rest_collector_age_seconds"] = round(rest_collector_age, 1) if rest_collector_age is not None else None
    report["trade_collector_age_seconds"] = round(trade_collector_age, 1) if trade_collector_age is not None else None

    # Attach first/last observation times per market for provenance.
    for m in report["per_market"]:
        slug = m["market_slug"]
        m["first_rest_observation"] = rest_first.get(slug).isoformat() if rest_first.get(slug) else None
        m["last_rest_observation"] = rest_last.get(slug).isoformat() if rest_last.get(slug) else None
        m["first_l2_observation"] = l2_first.get(slug).isoformat() if l2_first.get(slug) else None
        m["last_l2_observation"] = l2_last.get(slug).isoformat() if l2_last.get(slug) else None
        m["first_trade_observation"] = trade_first.get(slug).isoformat() if trade_first.get(slug) else None

    persist_mapping(required, args.mapping)

    report["checked_at"] = datetime.now(UTC).isoformat()
    report["mapping_file"] = args.mapping
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2))

    # Fail loudly if the next release is not fully covered.
    if report["next_release_id"] is not None and not report["next_release_coverage_ok"]:
        print(f"\nFAIL: next release {report['next_release_id']} is not fully covered", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
