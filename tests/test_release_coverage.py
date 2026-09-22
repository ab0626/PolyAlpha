"""Tests for the release-required universe and the coverage invariant."""

import json
import sys
from datetime import UTC, datetime

sys.path.insert(0, "src")

from polyalpha.us.release_coverage import (
    RequiredMarket,
    compute_coverage,
    merged_required_slugs,
    stratified_universe,
    stratum_for,
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


def test_stratum_for_category_and_prefix():
    strata = {
        "sports": {"categories": ["sports"], "slug_prefixes": ["aec-", "tec-"]},
        "crypto": {"categories": ["crypto"], "slug_prefixes": ["cpc-"]},
        "macro": {"categories": ["macro"], "slug_prefixes": ["nfpc-"]},
        "other": {"categories": [], "slug_prefixes": []},
    }
    assert stratum_for("aec-nba-x", "sports", strata) == "sports"
    assert stratum_for("cpc-btc-x", "crypto", strata) == "crypto"
    assert stratum_for("nfpc-x", None, strata) == "macro"  # prefix fallback
    assert stratum_for("unknown-x", None, strata) == "other"


def test_stratified_universe_deterministic_and_gated():
    class FakeClient:
        def markets(self, params):
            return ({"markets": [
                {"slug": "aec-a", "category": "sports", "active": True, "accepting_orders": True, "volume": 100},
                {"slug": "aec-b", "category": "sports", "active": True, "accepting_orders": True, "volume": 50},
                {"slug": "cpc-a", "category": "crypto", "active": True, "accepting_orders": True, "volume": 200},
                {"slug": "cpc-b", "category": "crypto", "active": False, "accepting_orders": True, "volume": 300},
            ]}, None)

    cfg = {
        "category_strata": {
            "sports": {"categories": ["sports"], "slug_prefixes": []},
            "crypto": {"categories": ["crypto"], "slug_prefixes": []},
            "other": {"categories": [], "slug_prefixes": []},
        },
        "sampling": {"count_per_stratum": 1, "min_active": True,
                     "min_accepting_orders": True, "min_volume": 0,
                     "sort_key": "volume", "descending": True},
    }
    sampled = stratified_universe(FakeClient(), cfg)
    slugs = {s for s, _ in sampled}
    assert "aec-a" in slugs      # top-volume sports
    assert "cpc-a" in slugs      # top-volume crypto
    assert "cpc-b" not in slugs  # inactive -> excluded by data-quality gate
    assert len(sampled) == 2     # one per stratum (sports + crypto)


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
