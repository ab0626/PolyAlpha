"""Tests for information-incorporation primitives: the fill-vs-quote race
measurement and the R(h) reaction curve."""

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, "src")

from polyalpha.information import incorporation_curve, logit, logit_delta
from polyalpha.pipeline import ExtractedEntity, compute_claim_id
from polyalpha.us.fills import FillRecord
from polyalpha.us.quote_race import Quote, measure_quote_race
from polyalpha.venue import Venue, is_executable, is_research_only

T = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _fill(slug, price, side, t):
    return FillRecord(
        market_slug=slug,
        price=D(str(price)),
        size=D("10"),
        trade_time=t,
        received_at=t,
        maker_side="SELL" if side == "BUY" else "BUY",
        maker_intent=None,
        taker_side=side,
        taker_intent="ORDER_INTENT_BUY_LONG" if side == "BUY" else "ORDER_INTENT_SELL_LONG",
    )


def test_quote_race_detects_stale_quote_before_revision():
    fills = [_fill("m1", "0.50", "BUY", T)]
    quotes = [
        Quote("m1", T - timedelta(seconds=1), D("0.48"), D("0.50")),  # pre ask 0.50
        Quote("m1", T + timedelta(seconds=1), D("0.53"), D("0.55")),  # ask revised up
    ]
    result = measure_quote_race(fills, quotes)
    assert result.total_fills == 1
    assert result.stale_fills == 1
    assert result.race_fills == 1
    assert abs(result.fraction - 1.0) < 1e-9


def test_quote_race_does_not_count_no_move():
    fills = [_fill("m1", "0.50", "BUY", T)]
    quotes = [
        Quote("m1", T - timedelta(seconds=1), D("0.48"), D("0.50")),
        Quote("m1", T + timedelta(seconds=1), D("0.48"), D("0.50")),  # no revision
    ]
    result = measure_quote_race(fills, quotes)
    assert result.race_fills == 0
    assert result.stale_fills == 1


def test_logit_and_delta():
    assert abs(logit(0.5)) < 1e-12
    assert abs(logit_delta(0.5, 0.55) - (logit(0.55) - logit(0.5))) < 1e-12


def test_incorporation_curve_normalizes_in_log_odds():
    series = [
        (T - timedelta(seconds=60), D("0.50")),
        (T + timedelta(seconds=5), D("0.52")),
        (T + timedelta(seconds=30), D("0.55")),
        (T + timedelta(seconds=300), D("0.58")),
    ]
    curve = incorporation_curve(series, T, horizons_seconds=[5, 30], stable_seconds=300)
    num5 = logit(0.52) - logit(0.50)
    num30 = logit(0.55) - logit(0.50)
    den = logit(0.58) - logit(0.50)
    assert abs(curve[5] - num5 / den) < 1e-9
    assert abs(curve[30] - num30 / den) < 1e-9


def test_incorporation_curve_flat_move_is_zero():
    series = [
        (T - timedelta(seconds=60), D("0.50")),
        (T + timedelta(seconds=300), D("0.50")),
    ]
    curve = incorporation_curve(series, T, horizons_seconds=[5], stable_seconds=300)
    assert curve[5] == 0.0


def test_claim_id_is_source_independent():
    entities = (ExtractedEntity("organization", "Fed", D("1.0")),)
    a = compute_claim_id(entities, "Fed cuts rates 25bp")
    b = compute_claim_id(entities, "Fed cuts rates 25bp")
    assert a == b
    # Whitespace normalization makes phrasings with different spacing equal.
    c = compute_claim_id(entities, "  Fed   cuts rates   25bp  ")
    assert a == c


def test_venue_split_is_hard_coded():
    assert is_executable(Venue.POLYMARKET_US)
    assert is_executable(Venue.KALSHI)
    assert not is_executable(Venue.POLYMARKET_INTERNATIONAL)
    assert is_research_only(Venue.POLYMARKET_INTERNATIONAL)
    assert not is_research_only(Venue.POLYMARKET_US)
