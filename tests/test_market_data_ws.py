"""Tests for the market-data WS parser, state machine, and feed resolution."""

import sys
from datetime import UTC, datetime

sys.path.insert(0, "src")

from polyalpha.us.market_data_ws import (
    MarketDataStream,
    parse_market_data,
    resolvable_horizons,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _l2(slug, bids, offers, tt="2026-09-21T12:00:00Z"):
    return {
        "requestId": "x",
        "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
        "marketData": {
            "marketSlug": slug,
            "bids": [{"px": {"value": str(b), "currency": "USD"}, "qty": str(q)} for b, q in bids],
            "offers": [{"px": {"value": str(a), "currency": "USD"}, "qty": str(q)} for a, q in offers],
            "state": "MARKET_STATE_OPEN",
            "transactTime": tt,
        },
    }


def _lite(slug, best_bid, best_ask):
    return {
        "requestId": "x",
        "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
        "marketDataLite": {
            "marketSlug": slug,
            "bestBid": {"value": str(best_bid), "currency": "USD"},
            "bestAsk": {"value": str(best_ask), "currency": "USD"},
        },
    }


def test_parse_l2():
    update = parse_market_data(_l2("m1", [(0.48, 100)], [(0.50, 100)]))
    assert update.kind == "L2"
    assert update.market_slug == "m1"
    assert update.bids[0][0] == __import__("decimal").Decimal("0.48")
    assert update.state is not None
    assert update.transact_time is not None


def test_parse_lite():
    update = parse_market_data(_lite("m1", 0.48, 0.50))
    assert update.kind == "BBO"
    assert update.market_slug == "m1"


def test_stream_deterministic_snapshot_and_reconnect_stale():
    stream = MarketDataStream()
    stream.ingest(_l2("m1", [(0.48, 100)], [(0.50, 100)]), NOW)
    assert not stream.books["m1"].stale

    # Reconnect marks every book stale until re-baselined by a fresh snapshot.
    stream.on_reconnect("session-2")
    assert stream.gap_count == 1
    assert stream.books["m1"].stale
    assert stream.stale_slugs == ["m1"]

    # A fresh full snapshot re-baselines (deterministic, never stitched).
    stream.ingest(_l2("m1", [(0.49, 120)], [(0.51, 120)]), NOW)
    assert not stream.books["m1"].stale
    assert stream.books["m1"].update.bids[0][0] == __import__("decimal").Decimal("0.49")


def test_stream_heartbeat():
    stream = MarketDataStream()
    stream.on_heartbeat(NOW)
    assert stream.last_heartbeat_at == NOW


def test_resolvable_horizons_respects_feed():
    assert resolvable_horizons("REST_2S") == [2000, 5000, 10000, 30000]
    # WS_L2 measured ~100ms cadence (smoke test), so 100ms+ is resolvable.
    assert resolvable_horizons("WS_L2") == [100, 250, 500, 1000, 2000, 5000, 10000, 30000]
    assert 100 in resolvable_horizons("WS_L2")
    assert 100 not in resolvable_horizons("REST_2S")
