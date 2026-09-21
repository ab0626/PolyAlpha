"""Tests for EventShock records and the per-market reaction measurement."""

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

sys.path.insert(0, "src")

from polyalpha.event_shock import (
    EventShock,
    MarketLink,
    Relationship,
    measure_shock_reaction,
)
from polyalpha.information import logit
from polyalpha.us.fills import FillRecord
from polyalpha.us.quote_race import Quote

T0 = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)


def _shock():
    return EventShock(
        shock_id="s1",
        event_id="cpi-2026-10-14",
        category="macro",
        source="bls",
        source_authority=1.0,
        primary_source=True,
        first_public_at=T0,
        first_received_at=T0 + timedelta(milliseconds=250),
        first_processed_at=T0 + timedelta(milliseconds=900),
        surprise_score=0.6,
        novelty_score=0.8,
        ambiguity_score=0.1,
        relevance_score=0.9,
        affected_markets=(
            MarketLink("m1", Relationship.PRIMARY, 1.0),
            MarketLink("m2", Relationship.DIRECT, 0.7),
        ),
    )


def _quote(slug, ts, bid, ask):
    return Quote(slug, ts, D(str(bid)), D(str(ask)))


def _fill(slug, ts):
    return FillRecord(
        market_slug=slug, price=D("0.50"), size=D("10"),
        trade_time=ts, received_at=ts,
        maker_side="SELL", maker_intent=None, taker_side="BUY",
        taker_intent="ORDER_INTENT_BUY_LONG",
    )


def test_event_shock_derived_latencies():
    s = _shock()
    assert s.receive_latency_ms == 250
    assert s.processing_latency_ms == 650
    assert s.total_internal_latency_ms == 900


def test_event_shock_rejects_out_of_range_scores():
    with pytest.raises(ValueError):
        EventShock(
            shock_id="x", event_id="e", category="macro", source="s",
            source_authority=1.0, primary_source=True,
            first_public_at=T0, first_received_at=T0, first_processed_at=T0,
            surprise_score=1.5,
        )


def test_event_shock_rejects_out_of_range_authority():
    with pytest.raises(ValueError):
        EventShock(
            shock_id="x", event_id="e", category="macro", source="s",
            source_authority=2.0, primary_source=True,
            first_public_at=T0, first_received_at=T0, first_processed_at=T0,
        )


def test_measure_shock_reaction_computes_curve_and_window():
    shock = _shock()
    # pre 0.50 -> stable 0.58 (logit move up).
    quotes = [
        _quote("m1", T0 - timedelta(seconds=60), "0.48", "0.50"),
        _quote("m1", T0 + timedelta(seconds=5), "0.53", "0.55"),    # ~60% of move
        _quote("m1", T0 + timedelta(seconds=30), "0.56", "0.58"),   # ~full move
        _quote("m1", T0 + timedelta(seconds=1800), "0.56", "0.58"), # stable
    ]
    fills = [_fill("m1", T0 + timedelta(seconds=4))]
    reaction = measure_shock_reaction(shock, "m1", quotes, fills, stable_seconds=1800)

    pre = logit(0.49)          # mid of bid 0.48 / ask 0.50
    total = logit(0.57) - pre  # stable mid of bid 0.56 / ask 0.58
    assert abs(reaction.pre_event_logit - pre) < 1e-9
    assert abs(reaction.eventual_logit_delta - total) < 1e-9
    # First quote move >= 0.02 logit: the 5s quote (0.53 vs pre 0.50).
    assert reaction.first_quote_move_at == T0 + timedelta(seconds=5)
    assert reaction.first_fill_at == T0 + timedelta(seconds=4)
    # W_quote = first_quote_move - processed = 5s - 0.9s = 4100ms.
    assert reaction.W_quote_ms == 4100
    # t50 ~ first time frac >= 0.5: the 5s quote.
    assert reaction.t50 == T0 + timedelta(seconds=5)
    # t10/t25/t75/t90 resolvable (all within the 5s quote or the 30s quote).
    assert reaction.t10 is not None
    assert reaction.t90 == T0 + timedelta(seconds=30)


def test_measure_shock_reaction_no_move_flags():
    shock = _shock()
    quotes = [
        _quote("m1", T0 - timedelta(seconds=60), "0.50", "0.52"),
        _quote("m1", T0 + timedelta(seconds=1800), "0.50", "0.52"),
    ]
    reaction = measure_shock_reaction(shock, "m1", quotes, [])
    assert "NO_MOVE" in reaction.data_quality_flags
    assert reaction.t50 is None
