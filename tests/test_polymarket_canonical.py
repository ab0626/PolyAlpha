"""Canonicalization/integrity tests for the Polymarket CLOB adapter (frozen)."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from polyalpha.polymarket.canonical import (
    BOUNDARY_MS,
    TICK_SIZE,
    off_tick,
    partition_of,
    parse_book,
    parse_trade,
    yes_direction,
)

D = Decimal


def _cj(d):
    return json.dumps(d, sort_keys=True)


def _book(slug="slug", bids=None, offers=None, venue=1_000_000, recv=2000, proc=3000, seq=7):
    md = {"marketSlug": slug, "bids": bids, "offers": offers, "state": "MARKET_STATE_OPEN"}
    inner = {"exchange_timestamp_ms": venue, "received_at_ns": recv, "processed_at_ns": proc,
             "message_sequence_local": seq, "payload": {"marketData": md}}
    cj = _cj(inner)
    outer = {"canonical_json": cj, "sha256": hashlib.sha256(cj.encode()).hexdigest()}
    return outer, inner, cj


def _lvl(px, qty):
    return {"px": {"currency": "USD", "value": str(px)}, "qty": str(qty)}


def _trade(outcome="OUTCOME_SIDE_YES", side="ORDER_SIDE_BUY", intent="ORDER_INTENT_BUY_LONG",
           maker="ORDER_SIDE_SELL", price="0.5", qty="10", tid="t1"):
    tr = {"id": tid, "marketSlug": "slug",
          "price": {"currency": "USD", "value": price},
          "quantity": {"currency": "USD", "value": qty},
          "tradeTime": "2026-09-22T00:00:00.000Z",
          "taker": {"outcomeSide": outcome, "side": side, "intent": intent},
          "maker": {"side": maker}}
    inner = {"payload": {"trade": tr}}
    cj = _cj(inner)
    outer = {"canonical_json": cj, "sha256": hashlib.sha256(cj.encode()).hexdigest()}
    return outer, inner, cj


def test_sha256_verify_success_and_failure():
    outer, inner, cj = _book(bids=[_lvl("0.4", "100")], offers=[_lvl("0.5", "100")])
    assert parse_book(outer, inner, cj).integrity_verified is True
    assert parse_book(outer, inner, cj).gap_clean is True
    outer["sha256"] = "0" * 64
    b = parse_book(outer, inner, cj)
    assert b.integrity_verified is False and b.gap_clean is False


def test_native_bids_offers_preserved_and_sorted():
    outer, inner, cj = _book(
        bids=[_lvl("0.42", "200"), _lvl("0.40", "100"), _lvl("0.45", "50")],
        offers=[_lvl("0.50", "300"), _lvl("0.48", "150")],
    )
    b = parse_book(outer, inner, cj)
    assert b.bids == ((D("0.40"), D("100")), (D("0.42"), D("200")), (D("0.45"), D("50")))
    assert b.offers == ((D("0.48"), D("150")), (D("0.50"), D("300")))
    assert b.best_bid == D("0.45") and b.best_ask == D("0.48")
    assert b.mid == D("0.465") and b.spread == D("0.03")
    assert b.top5_bid_depth == D("350") and b.top5_ask_depth == D("450")


def test_tick_validation():
    assert off_tick(D("0.138")) is False
    assert off_tick(D("0.010")) is False
    assert off_tick(D("0.1385")) is True


def test_one_sided_and_crossed_fail_closed():
    outer, inner, cj = _book(bids=[_lvl("0.4", "100")], offers=[])
    assert parse_book(outer, inner, cj) is None
    outer2, inner2, cj2 = _book(bids=[_lvl("0.6", "100")], offers=[_lvl("0.4", "100")])
    assert parse_book(outer2, inner2, cj2) is None


def test_sequence_discontinuity_does_not_affect_gap_clean():
    outer, inner, cj = _book(bids=[_lvl("0.4", "100")], offers=[_lvl("0.5", "100")], seq=999999)
    b = parse_book(outer, inner, cj)
    assert b.integrity_verified is True and b.gap_clean is True
    assert b.message_sequence_local == 999999


def test_timestamps_distinct_and_partition():
    outer, inner, cj = _book(bids=[_lvl("0.4", "100")], offers=[_lvl("0.5", "100")],
                             venue=BOUNDARY_MS + 5, recv=111, proc=222)
    b = parse_book(outer, inner, cj)
    assert (b.exchange_timestamp_ms, b.received_at_ns, b.processed_at_ns) == (BOUNDARY_MS + 5, 111, 222)
    assert b.partition == "forward"
    assert partition_of(BOUNDARY_MS - 1) == "dev"


def test_deterministic_byte_equivalent_output():
    outer, inner, cj = _book(bids=[_lvl("0.40", "100"), _lvl("0.45", "50")], offers=[_lvl("0.50", "300")])
    a = parse_book(outer, inner, cj).to_record()
    b = parse_book(outer, inner, cj).to_record()
    assert a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_yes_direction_and_outcome_mapping():
    assert yes_direction("YES", "BUY") == 1
    assert yes_direction("YES", "SELL") == -1
    assert yes_direction("NO", "BUY") == -1
    assert yes_direction("NO", "SELL") == 1


def test_trade_classification_complete_and_unknown():
    outer, inner, cj = _trade()
    t = parse_trade(outer, inner, cj)
    assert t.classification_status == "COMPLETE"
    assert t.maker_taker_complete is True
    assert t.yes_direction == 1
    # missing outcome -> UNKNOWN/ineligible, never inferred
    outer2, inner2, cj2 = _trade(outcome="OUTCOME_SIDE_UNSPECIFIED")
    t2 = parse_trade(outer2, inner2, cj2)
    assert t2.classification_status == "MISSING_OUTCOME"
    assert t2.maker_taker_complete is False
    assert t2.yes_direction == 0


def test_trade_timestamps_and_serialization():
    outer, inner, cj = _trade()
    t = parse_trade(outer, inner, cj)
    assert t.trade_time_ms > 0
    assert t.price == D("0.5") and t.size == D("10")
    assert json.dumps(t.to_record(), sort_keys=True) == json.dumps(parse_trade(outer, inner, cj).to_record(), sort_keys=True)