"""Invariant tests for the Polymarket canonical parser (frozen contract)."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from polyalpha.polymarket.canonical import (
    FORWARD_BOUNDARY,
    BOUNDARY_MS,
    partition_of,
    parse_book,
    parse_trade,
    yes_direction,
)

D = Decimal


def _book_outer(slug="slug", bids=None, offers=None, venue=1_000_000, recv=2_000, proc=3_000):
    md = {"marketSlug": slug, "bids": bids, "offers": offers, "state": "MARKET_STATE_OPEN"}
    inner = {
        "exchange_timestamp_ms": venue, "received_at_ns": recv, "processed_at_ns": proc,
        "payload": {"marketData": md},
    }
    cj = json.dumps(inner, sort_keys=True)
    outer = {"canonical_json": cj, "sha256": hashlib.sha256(cj.encode()).hexdigest()}
    return outer, inner, cj


def _lvl(px, qty):
    return {"px": {"currency": "USD", "value": str(px)}, "qty": str(qty)}


def _trade_outer(taker_outcome="OUTCOME_SIDE_YES", taker_side="ORDER_SIDE_BUY",
                 taker_intent="ORDER_INTENT_BUY_LONG", maker_side="ORDER_SIDE_SELL",
                 price="0.5", qty="10"):
    tr = {
        "id": "t1", "marketSlug": "slug",
        "price": {"currency": "USD", "value": price},
        "quantity": {"currency": "USD", "value": qty},
        "tradeTime": "2026-09-22T00:00:00.000Z",
        "taker": {"outcomeSide": taker_outcome, "side": taker_side, "intent": taker_intent},
        "maker": {"side": maker_side},
    }
    inner = {"payload": {"trade": tr}}
    cj = json.dumps(inner, sort_keys=True)
    outer = {"canonical_json": cj, "sha256": hashlib.sha256(cj.encode()).hexdigest()}
    return outer, inner, cj


def test_native_bid_offer_semantics_no_complement():
    outer, inner, cj = _book_outer(
        bids=[_lvl("0.40", "100"), _lvl("0.42", "200"), _lvl("0.45", "50")],
        offers=[_lvl("0.48", "150"), _lvl("0.50", "300")],
    )
    b = parse_book(outer, inner, cj)
    assert b.best_bid == D("0.45")      # max bid (native, no complement)
    assert b.best_ask == D("0.48")      # min offer
    assert b.mid == D("0.465")
    assert b.spread == D("0.03")
    assert b.bid_depth_k == D("350")     # 50 + 200 + 100
    assert b.ask_depth_k == D("450")     # 150 + 300


def test_timestamps_distinct_and_partition():
    outer, inner, cj = _book_outer(bids=[_lvl("0.4", "100")], offers=[_lvl("0.5", "100")],
                                   venue=BOUNDARY_MS + 1000, recv=111, proc=222)
    b = parse_book(outer, inner, cj)
    assert b.venue_ts_ms == BOUNDARY_MS + 1000
    assert b.receive_ts_ns == 111
    assert b.processed_ts_ns == 222
    assert b.partition == "forward"


def test_hash_break_invalidates():
    outer, inner, cj = _book_outer(bids=[_lvl("0.4", "100")], offers=[_lvl("0.5", "100")])
    outer["sha256"] = "deadbeef"
    b = parse_book(outer, inner, cj)
    assert b.gap_clean is False


def test_impossible_book_fails_closed():
    # best_bid >= best_ask
    outer, inner, cj = _book_outer(bids=[_lvl("0.6", "100")], offers=[_lvl("0.4", "100")])
    assert parse_book(outer, inner, cj) is None
    # one-sided
    outer2, inner2, cj2 = _book_outer(bids=[_lvl("0.4", "100")], offers=[])
    assert parse_book(outer2, inner2, cj2) is None


def test_deterministic_serialization():
    outer, inner, cj = _book_outer(bids=[_lvl("0.40", "100"), _lvl("0.45", "50")],
                                   offers=[_lvl("0.50", "300")])
    b1 = parse_book(outer, inner, cj)
    b2 = parse_book(outer, inner, cj)
    assert b1.to_record() == b2.to_record()


def test_yes_direction():
    assert yes_direction("YES", "BUY") == 1
    assert yes_direction("YES", "SELL") == -1
    assert yes_direction("NO", "BUY") == -1
    assert yes_direction("NO", "SELL") == 1


def test_trade_parse_and_classification():
    outer, inner, cj = _trade_outer()
    t = parse_trade(outer, inner, cj)
    assert t is not None
    assert t.yes_direction == 1
    assert t.taker_intent == "BUY_LONG"
    assert t.price == D("0.5") and t.size == D("10")


def test_partition_boundary():
    assert partition_of(BOUNDARY_MS - 1) == "dev"
    assert partition_of(BOUNDARY_MS) == "forward"
    assert partition_of(BOUNDARY_MS + 1) == "forward"