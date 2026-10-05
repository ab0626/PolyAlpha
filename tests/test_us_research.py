"""Tests for the Polymarket US research dataset builder.

Covers: raw -> point-in-time snapshots, book BBO extraction, parent-event
clustering, settlement label attachment, and the sampling-hierarchy (effective
N) computation. All offline, using synthetic raw records written through the
(real) RawStore.
"""

import json
import sys

sys.path.insert(0, "src")

from polyalpha.rawstore import RawStore
from polyalpha.us.forward_evidence import _observed_coverage
from polyalpha.us.research import build_us_research_dataset, sampling_hierarchy


def _market(slug, category, question, end="2026-10-01T00:00:00Z"):
    return {
        "slug": slug,
        "category": category,
        "question": question,
        "endDate": end,
        "marketSides": [{"long": True, "price": "0.5"}, {"long": False, "price": "0.5"}],
    }


def _book(slug, bid="0.50", ask="0.52", traded="1000", open_interest="5000"):
    return {
        "marketData": {
            "marketSlug": slug,
            "bids": [{"px": {"value": bid, "currency": "USD"}, "qty": "100"}],
            "offers": [{"px": {"value": ask, "currency": "USD"}, "qty": "100"}],
            "state": "MARKET_STATE_OPEN",
            "stats": {"sharesTraded": traded, "openInterest": open_interest},
            "transactTime": "2026-09-15T12:00:00Z",
        }
    }


def _store(tmp_path):
    raw = RawStore(tmp_path / "raw", "test")
    raw.append("polymarket_us_retail_markets", "discovery", {"markets": [
        _market("m1", "sports", "Will team A win?"),
        _market("m2", "politics", "Will candidate B win?"),
    ]}, received_at_ns=1_000)
    raw.append("polymarket_us_retail_events", "events", {"events": [
        {"id": "e1", "markets": [{"slug": "m1"}, {"slug": "m2"}]},
    ]}, received_at_ns=2_000)
    raw.append("polymarket_us_retail_book", "m1", _book("m1", "0.50", "0.52"), received_at_ns=3_000)
    raw.append("polymarket_us_retail_book", "m2", _book("m2", "0.60", "0.64"), received_at_ns=4_000)
    raw.append("polymarket_us_retail_settlement", "m1", {"slug": "m1", "settlement": 1}, received_at_ns=5_000)
    raw.append("polymarket_us_retail_settlement", "m2", {"slug": "m2", "settlement": 0}, received_at_ns=6_000)
    raw.close()
    return tmp_path / "raw"


def test_build_dataset_and_bbo(tmp_path):
    ds = build_us_research_dataset(_store(tmp_path))
    assert ds.total_observations == 2
    m1 = [s for s in ds.snapshots if s.market_id == "us:m1"][0]
    m2 = [s for s in ds.snapshots if s.market_id == "us:m2"][0]

    assert m1.yes_mid is not None
    assert float(m1.yes_mid) == 0.51  # (0.50 + 0.52) / 2
    assert float(m1.yes_spread) == 0.02
    assert m1.category == "sports"
    assert m2.category == "politics"
    assert float(m2.yes_mid) == 0.62


def test_parent_event_clustering_and_labels(tmp_path):
    ds = build_us_research_dataset(_store(tmp_path))
    # Both markets share the parent event e1.
    assert {s.event_cluster for s in ds.snapshots} == {"e1"}
    assert ds.unique_clusters == 1
    by_market = {s.market_id: s for s in ds.snapshots}
    assert by_market["us:m1"].final_resolution == 1
    assert by_market["us:m2"].final_resolution == 0
    # Resolution timestamp is populated (when the outcome became known).
    assert by_market["us:m1"].resolution_timestamp is not None


def test_sampling_hierarchy_effective_n(tmp_path):
    ds = build_us_research_dataset(_store(tmp_path))
    h = sampling_hierarchy(ds)
    assert h["raw_observations"] == 2
    assert h["unique_markets"] == 2
    assert h["parent_event_clusters"] == 1
    assert h["effective_n"] == 1  # one parent event, not two rows
    assert h["resolved_markets"] == 2


def test_non_binary_settlement_is_not_a_label(tmp_path):
    raw = RawStore(tmp_path / "raw", "test")
    raw.append("polymarket_us_retail_markets", "d", {"markets": [_market("m1", "sports", "Q")]}, received_at_ns=1)
    raw.append("polymarket_us_retail_book", "m1", _book("m1"), received_at_ns=2)
    raw.append("polymarket_us_retail_settlement", "m1", {"slug": "m1", "settlement": 0.5}, received_at_ns=3)
    raw.close()
    ds = build_us_research_dataset(tmp_path / "raw")
    assert ds.snapshots[0].final_resolution is None  # split/void is not binary
    assert ds.resolved_observations == 0


def test_macro_release_clustering_from_mapping(tmp_path):
    # Two CPI buckets are one parent event (the release), never two independent
    # clusters. The macro slugs are absent from the retail events feed, so the
    # release mapping must supply the parent grouping.
    slugs = [
        "cpic-uscpi-yoy-2026-10-14-gt2pt9pct",
        "cpic-uscpi-yoy-2026-10-14-gt3pt0pct",
    ]
    raw = RawStore(tmp_path / "raw", "test")
    raw.append("polymarket_us_retail_markets", "d",
               {"markets": [_market(s, "macro", "CPI YoY") for s in slugs]},
               received_at_ns=1)
    for i, slug in enumerate(slugs):
        raw.append("polymarket_us_retail_book", slug, _book(slug), received_at_ns=2 + i)
    raw.close()

    mapping = tmp_path / "release_mapping.jsonl"
    mapping.write_text(
        "".join(
            json.dumps({"market_slug": s, "release_id": "cpi-2026-10-14"}) + "\n"
            for s in slugs
        ),
        encoding="utf-8",
    )

    ds = build_us_research_dataset(tmp_path / "raw", release_mapping_path=mapping)
    assert {s.event_cluster for s in ds.snapshots} == {"cpi-2026-10-14"}
    assert sampling_hierarchy(ds)["effective_n"] == 1


def test_release_mapping_does_not_override_events_feed(tmp_path):
    # A slug that already has a retail parent event keeps it; the release
    # mapping only fills gaps (events feed wins on collision).
    raw = RawStore(tmp_path / "raw", "test")
    raw.append("polymarket_us_retail_markets", "d",
               {"markets": [_market("m1", "sports", "Q")]}, received_at_ns=1)
    raw.append("polymarket_us_retail_events", "events",
               {"events": [{"id": "e1", "markets": [{"slug": "m1"}]}]}, received_at_ns=2)
    raw.append("polymarket_us_retail_book", "m1", _book("m1"), received_at_ns=3)
    raw.close()

    mapping = tmp_path / "release_mapping.jsonl"
    mapping.write_text(
        json.dumps({"market_slug": "m1", "release_id": "some-release"}) + "\n",
        encoding="utf-8",
    )

    ds = build_us_research_dataset(tmp_path / "raw", release_mapping_path=mapping)
    assert {s.event_cluster for s in ds.snapshots} == {"e1"}


def test_observed_coverage_unions_overlapping_gaps():
    from datetime import UTC, datetime

    as_of = datetime(2026, 10, 6, tzinfo=UTC)
    # Two 4-day gaps that overlap by 2 days: union is 6 days, not 8.
    gaps = [
        {"start": "2026-09-17T00:00:00+00:00", "end": "2026-09-21T00:00:00+00:00"},
        {"start": "2026-09-19T00:00:00+00:00", "end": "2026-09-23T00:00:00+00:00"},
    ]
    cov = _observed_coverage(as_of, gaps)
    # Nominal window: 2026-09-16T02:49:12 -> 2026-10-06T00:00 ≈ 20.88 days.
    # Union gap: 2026-09-17 -> 2026-09-23 = 6 days.
    expected = 1.0 - (6 * 86400) / ((as_of - datetime(2026, 9, 16, 2, 49, 12, tzinfo=UTC)).total_seconds())
    assert abs(cov - expected) < 1e-9, (cov, expected)
    # Sanity: the union must be smaller than the naive double-counted sum.
    naive = 1.0 - (8 * 86400) / ((as_of - datetime(2026, 9, 16, 2, 49, 12, tzinfo=UTC)).total_seconds())
    assert cov > naive
    assert _observed_coverage(as_of, []) == 1.0
