"""Tests for the US order-flow layer: fill parsing, signed-flow mapping, the
trade-stream dedupe core, and the derived OFI/markout signals. All offline."""

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, "src")

from polyalpha.us.fill_signals import mean_markout, signed_flow_series
from polyalpha.us.fills import parse_us_trade
from polyalpha.us.trade_stream import UsTradeStream

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _msg(taker_side="BUY", taker_intent="ORDER_INTENT_BUY_LONG", price="0.55", qty="100", trade_time="2026-09-21T12:00:00Z"):
    return {
        "type": "SUBSCRIPTION_TYPE_TRADE",
        "marketSlug": "m1",
        "price": price,
        "quantity": qty,
        "tradeTime": trade_time,
        "maker": {"side": "SELL", "intent": "ORDER_INTENT_MAKER_SELL_LONG"},
        "taker": {"side": taker_side, "intent": taker_intent},
        "tradeId": "t1",
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
    raw.pop("taker")
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
            _msg(taker_side="SELL", taker_intent="ORDER_INTENT_SELL_LONG", qty="10",
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
