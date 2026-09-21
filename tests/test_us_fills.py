"""Tests for the US order-flow layer: fill parsing, signed-flow mapping, the
trade-stream dedupe core, and the derived OFI/markout signals. All offline."""

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, "src")

from polyalpha.us.fill_signals import (
    kyle_lambda_ols,
    mean_markout,
    side_agreement_rate,
    signed_flow_series,
)
from polyalpha.us.fills import parse_us_trade
from polyalpha.us.trade_stream import UsTradeStream

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _msg(taker_side="ORDER_SIDE_BUY", taker_intent="ORDER_INTENT_BUY_LONG", price="0.55", qty="100", trade_time="2026-09-21T12:00:00Z"):
    # Shape mirrors the polymarket-us SDK websocket/types.py Trade message.
    return {
        "requestId": "trade-sub-1",
        "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
        "trade": {
            "marketSlug": "m1",
            "price": {"value": price, "currency": "USD"},
            "quantity": {"value": qty, "currency": "USD"},
            "tradeTime": trade_time,
            "maker": {"side": "ORDER_SIDE_SELL", "intent": "ORDER_INTENT_MAKER_SELL_LONG"},
            "taker": {"side": taker_side, "intent": taker_intent},
        },
    }


def test_parse_us_trade():
    record = parse_us_trade(_msg(), NOW)
    assert record.market_slug == "m1"
    assert record.price == D("0.55")
    assert record.size == D("100")
    assert record.taker_side == "BUY"
    assert record.aggressor_side == "BUY"
    assert record.signed_size == D("100")


def test_parse_rejects_missing_side():
    raw = _msg()
    raw["trade"].pop("taker")
    try:
        parse_us_trade(raw, NOW)
    except ValueError:
        return
    raise AssertionError("expected ValueError for missing taker side")


def test_signed_yes_flow_intent_mapping():
    cases = {
        "ORDER_INTENT_BUY_LONG": D("100"),
        "ORDER_INTENT_SELL_SHORT": D("100"),   # selling NO == buying YES
        "ORDER_INTENT_SELL_LONG": D("-100"),
        "ORDER_INTENT_BUY_SHORT": D("-100"),   # buying NO == selling YES
    }
    for intent, expected in cases.items():
        r = parse_us_trade(_msg(taker_intent=intent), NOW)
        assert r.signed_yes_flow == expected, intent


def test_trade_stream_dedupes():
    stream = UsTradeStream()
    first = stream.ingest(_msg(), NOW)
    assert first is not None
    assert stream.ingest(_msg(), NOW) is None  # identical message -> duplicate
    assert len(stream.records) == 1


def test_signed_flow_series_buckets():
    records = [
        parse_us_trade(_msg(price="0.50", qty="10", trade_time="2026-09-21T12:00:00Z"), NOW),
        parse_us_trade(_msg(price="0.50", qty="10", trade_time="2026-09-21T12:00:30Z"), NOW),
        parse_us_trade(
            _msg(taker_side="ORDER_SIDE_SELL", taker_intent="ORDER_INTENT_SELL_LONG", qty="10",
                 trade_time="2026-09-21T12:01:01Z"),
            NOW,
        ),
    ]
    series = signed_flow_series(records, window_seconds=60)
    # bucket 0: +10 +10 = 20 ; bucket 1: -10
    assert [float(s) for _, s in series] == [20.0, -10.0]


def test_mean_markout():
    records = [parse_us_trade(_msg(price="0.50"), NOW)]  # aggressive BUY
    prices = {NOW: D("0.50"), NOW + timedelta(seconds=60): D("0.48")}

    def price_fn(ts):
        return prices.get(ts)

    markout = mean_markout(records, price_fn, horizon_seconds=60)
    # sign=+1 (BUY), p_{t+h}-p_t = -0.02 -> adverse selection.
    assert markout == D("-0.02")


def test_kyle_lambda_ols():
    flow = [D("0"), D("1"), D("2"), D("3")]
    dp = [D("0"), D("0.5"), D("1.0"), D("1.5")]
    assert abs(kyle_lambda_ols(flow, dp) - 0.5) < 1e-9


def test_side_agreement_rate():
    a = ["BUY", "SELL", "BUY"]
    b = ["BUY", "BUY", "BUY"]
    assert abs(side_agreement_rate(a, b) - 2 / 3) < 1e-9
