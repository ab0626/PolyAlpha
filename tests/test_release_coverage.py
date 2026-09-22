"""Tests for the release-required universe and the coverage invariant."""

import json
import sys
from datetime import UTC, datetime

sys.path.insert(0, "src")

from polyalpha.us.release_coverage import (
    RequiredMarket,
    compute_coverage,
    merged_required_slugs,
)

T = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)


def test_compute_coverage_invariant():
    rm = [
        RequiredMarket("nfp-2026-10-02", "NFP", T, "s1", "us:s1"),
        RequiredMarket("nfp-2026-10-02", "NFP", T, "s2", "us:s2"),
    ]
    # s1 missing from REST -> invariant fails.
    report = compute_coverage(rm, {"s1"}, {"s1", "s2"}, {"s1", "s2"})
    assert report["required_markets_missing"] == 1
    assert report["required_markets_collecting"] == 1
    assert report["next_release_id"] == "nfp-2026-10-02"
    assert report["next_release_coverage_ok"] is False

    # Full coverage -> invariant holds.
    report = compute_coverage(rm, {"s1", "s2"}, {"s1", "s2"}, {"s1", "s2"})
    assert report["required_markets_missing"] == 0
    assert report["next_release_coverage_ok"] is True


def test_merged_required_slugs_retains_persisted_on_search_failure(tmp_path):
    mapping = tmp_path / "mapping.jsonl"
    mapping.write_text(
        json.dumps({
            "market_id": "us:s1", "market_slug": "s1",
            "release_id": "nfp-2026-10-02", "discovered_at": "x",
        }) + "\n",
        encoding="utf-8",
    )
    # Search returns nothing (transient failure), but the persisted slug survives.
    slugs = merged_required_slugs("config/gov_releases.json", lambda term: [], mapping)
    assert "s1" in slugs
